"""The `quickbeam build` CLI — the offline, one-shot ingestion driver.

Parses args, resolves the bundle/view graph generator, embeds each pending manifest
into Qdrant, and optionally projects a 2-D UMAP map afterwards. The live counterpart
is `watcher.py`, which reuses the same ingest primitives on a poll loop.
"""
import argparse
import asyncio
import json
import os

from qdrant_client import QdrantClient
from qdrant_client import models

from quickbeam.roles import infer_roles, role_map_applies
from quickbeam.ingest.checkpoint import _load_checkpoint, _save_checkpoint, _save_role_map
from quickbeam.ingest.embed import (
    MODEL_DIM_MAP, _init_embed_engine, ensure_indexes, _embed_and_upload,
)
from quickbeam.ingest.sources.ipfs import _cid_to_path
from quickbeam.ingest.umap import write_umap_coords
from quickbeam.ingest.graph.projection import _load_profiles
from quickbeam.ingest.graph.bundle import build_bundle_joined_data
from quickbeam.ingest.graph.view import build_view_joined_data


# ---------------------------------------------------------------------------
# CONFIG & ARGS
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Fangorn Qdrant Ingestion CLI Builder")
    parser.add_argument("--bundle", default=None,
                        help="Bundle schema as name=schemaId. Walks one publisher's typed graph.")
    parser.add_argument("--view", default=None,
                        help="Composed View as name=schemaId. Fuses the view's source datasources "
                             "into one graph (joins on Entity URI + aliases) before projecting.")
    parser.add_argument(
        "--root-profile",
        action="append",
        default=[],
        help="Named projection(s) to emit, repeatable: e.g. --root-profile track "
             "--root-profile place. Each profile walks the graph from a root type "
             "and emits a distinct document (see ROOT_PROFILES).",
    )
    parser.add_argument("--profiles-file", default=None,
                        help="Optional JSON file of custom/override root profiles, "
                             "merged over the built-in ROOT_PROFILES.")
    parser.add_argument("--max-depth", type=int, default=2,
                        help="Default traversal depth for profiles that don't set one.")
    parser.add_argument("--label-cap", type=int, default=50,
                        help="Max neighbor labels collected per relation group in a projection.")
    parser.add_argument("--node-cap", type=int, default=2000,
                        help="Max nodes a single root's graph walk will visit (cost bound).")
    parser.add_argument("--subgraph-url", default="https://gateway.thegraph.com/api/subgraphs/id/8SgbhtiitpAhEfyTgeAHxHH5DQ2gTygUuXgc3b7MCFyc")
    parser.add_argument("--graph-api-key", default="")
    parser.add_argument("--ipfs-gateway", default="https://gateway.pinata.cloud/ipfs")
    parser.add_argument("--ipfs-gateway-key", default=None)
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--qdrant-grpc-port", type=int, default=6334)
    parser.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    parser.add_argument("--collection", default="fangorn")
    parser.add_argument("--searchable-fields", default="auto")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--ipfs-timeout", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--reset", action="store_true", default=False)
    parser.add_argument("--embedding-model", default="nomic-ai/nomic-embed-text-v1.5")
    parser.add_argument("--dim", type=int, default=256, help="Matryoshka output dim: 256, 512, or 768")
    parser.add_argument("--embed-batch", type=int, default=16)
    parser.add_argument("--role-map-file", default="./db/role_map.json")
    parser.add_argument("--umap", action="store_true", default=False)
    parser.add_argument("--umap-only", action="store_true", default=False)
    parser.add_argument("--umap-tmp-dir", default="./db/umap_tmp",
                        help="Directory for UMAP temp memmaps. MUST be on real disk — "
                             "the system temp dir is often tmpfs (RAM), where the ~10GB "
                             "vector array would exhaust memory.")
    parser.add_argument("--umap-neighbors", type=int, default=15)
    parser.add_argument("--umap-min-dist", type=float, default=0.05)
    parser.add_argument("--umap-writeback-batch", type=int, default=1000,
                        help="Points per Qdrant payload-update call during UMAP write-back. "
                             "Lower it if Qdrant balloons RAM on a small machine.")
    parser.add_argument("--umap-writeback-sleep", type=float, default=0.0,
                        help="Seconds to pause between write-back batches (synchronous mode "
                             "only, i.e. --umap-writeback-workers 1). Throttles Qdrant on "
                             "tiny machines.")
    parser.add_argument("--umap-writeback-workers", type=int, default=4,
                        help="Concurrent connections applying UMAP px/py updates. >1 multiplies "
                             "write-back throughput ~Nx. wait=True per call still bounds Qdrant "
                             "memory. Set 1 for the old synchronous (throttle-able) path.")
    parser.add_argument("--umap-writeback-wait", default=True,
                        action=argparse.BooleanOptionalAction,
                        help="If true (default), each write waits for Qdrant to apply it — "
                             "safe but disk-flush bound. Pass --no-umap-writeback-wait for a "
                             "big speedup (async WAL writes + one final flush); ONLY with a "
                             "Qdrant memory cap, or the apply-queue can OOM the box.")
    parser.add_argument("--umap-target", choices=["file", "payload"], default="file",
                        help="Where to put the UMAP projection. 'file' (default) writes a "
                             "catalog-map artifact the server streams from /catalog/map — one "
                             "fast read pass, no Qdrant writes (the only path that scales to "
                             "millions). 'payload' writes px/py into each Qdrant point (slow; "
                             "needed only if you must bake coords into the Qdrant snapshot).")
    parser.add_argument("--umap-map-file", default="./db/catalog_map.json.gz",
                        help="Output path for the --umap-target file artifact (gzipped JSON).")
    parser.add_argument("--max-manifests", type=int, default=0,
                        help="Build at most N bundle manifests (shards) this run, then stop. "
                             "0 = no limit. Progress is checkpointed, so a later run resumes "
                             "with the next un-built shards. Use this to build a small number of "
                             "shards on a memory-limited machine.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# PIPELINE EXECUTION
# ---------------------------------------------------------------------------
async def main():
    args = parse_args()

    def _make_qdrant():
        return QdrantClient(
            host=args.qdrant_host, port=args.qdrant_port,
            grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=600
        )

    qdrant = _make_qdrant()

    if args.umap_only:
        if not qdrant.collection_exists(args.collection):
            print(f"[Builder] collection '{args.collection}' does not exist — nothing to project")
            return
        # The map artifact needs the role map (title/subtitle/tags field names) the
        # build wrote; load it from disk since umap-only doesn't run the join.
        umap_role_map = {}
        if os.path.exists(args.role_map_file):
            with open(args.role_map_file) as f:
                umap_role_map = json.load(f)
        write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist, tmp_dir=args.umap_tmp_dir, reconnect=_make_qdrant,
                          writeback_batch=args.umap_writeback_batch, writeback_sleep=args.umap_writeback_sleep,
                          writeback_workers=args.umap_writeback_workers,
                          writeback_wait=args.umap_writeback_wait,
                          target=args.umap_target, map_file=args.umap_map_file, role_map=umap_role_map)
        ensure_indexes(qdrant, args.collection)
        return

    model_dim = MODEL_DIM_MAP.get(args.embedding_model, 768)
    dim       = min(args.dim, model_dim)
    truncate  = dim < model_dim

    if not (args.bundle or args.view):
        print("[Builder] Nothing to do — pass --bundle NAME=0x... or --view NAME=0x... "
              "(or --umap-only to (re)project an existing collection).")
        return

    checkpoint = _load_checkpoint(args.checkpoint_file)
    completed_manifest_cids = set(checkpoint["completed_manifest_cids"])
    # processed_track_ids: only non-empty when a previous run crashed mid-manifest
    processed_track_ids = set(checkpoint["processed_track_ids"])

    # ── BUNDLE / VIEW PATH ───────────────────────────────────────────────────
    # Both walk a typed graph and project it; they differ only in the data
    # generator — a single source (bundle) vs. several fused sources (view).
    if args.view:
        b_name, b_id = args.view.split("=", 1)
        _mode = "View"
    else:
        b_name, b_id = args.bundle.split("=", 1)
        _mode = "Bundle"
    profiles = _load_profiles(args)
    _prof_desc = ", ".join(f"{p['name']}→{p['root_type']}" for p in profiles)
    print(f"\n[Builder] {_mode} mode: '{b_name.strip()}' — projections: {_prof_desc}")

    if args.reset and qdrant.collection_exists(args.collection):
        print(f"[Builder] Resetting collection '{args.collection}'...")
        qdrant.delete_collection(args.collection)
        completed_manifest_cids.clear()
        processed_track_ids.clear()
        checkpoint["completed_manifest_cids"] = []
        checkpoint["processed_track_ids"] = []

    role_map: dict = {}
    if os.path.exists(args.role_map_file):
        with open(args.role_map_file) as f:
            role_map = json.load(f)

    # Persist the checkpoint every CHECKPOINT_EVERY completed manifests rather
    # than after each one. Serializing the full (growing) completed-cid list +
    # manifests dict on every manifest is O(N) per write → O(N²) over a run of
    # ~10k manifests. Batching cuts that to O(N²/CHECKPOINT_EVERY). Safe because
    # point ids are deterministic: a crash that re-runs the manifests since the
    # last flush re-upserts them idempotently (no duplicates), only re-spending
    # the embed compute for at most CHECKPOINT_EVERY manifests.
    CHECKPOINT_EVERY = 50

    def _mark_complete(mcid: str) -> None:
        completed_manifest_cids.add(mcid)
        checkpoint["completed_manifest_cids"] = list(completed_manifest_cids)
        checkpoint["processed_track_ids"] = []
        processed_track_ids.clear()

    def _flush() -> None:
        _save_checkpoint(checkpoint, args.checkpoint_file)

    # Lazy-init: don't load the model into GPU VRAM until we know there's
    # actual new work to do (avoids OOM when everything is already checkpointed).
    embed_engine = None
    any_new = False
    manifest_num = 0
    since_flush  = 0

    if args.view:
        _data_gen = build_view_joined_data(
            args, b_id.strip(), profiles,
            completed_manifest_cids=completed_manifest_cids,
        )
    else:
        _data_gen = build_bundle_joined_data(
            args, b_id.strip(), profiles,
            completed_manifest_cids=completed_manifest_cids,
        )

    async for mcid, records in _data_gen:
        new_records = [r for r in records if r["track_id"] not in processed_track_ids]
        if not new_records:
            # All records in this manifest were already embedded — mark complete.
            _mark_complete(mcid)
            since_flush += 1
            if since_flush >= CHECKPOINT_EVERY:
                _flush()
                since_flush = 0
            continue

        # First manifest with real work: set up collection + model.
        if embed_engine is None:
            if not qdrant.collection_exists(args.collection):
                qdrant.create_collection(
                    args.collection,
                    vectors_config=models.VectorParams(
                        size=dim, distance=models.Distance.COSINE, on_disk=True)
                )
            ensure_indexes(qdrant, args.collection)
            embed_engine = _init_embed_engine(args)
            print(f"[Builder] model dim={model_dim}, output dim={dim} (truncate={truncate})")

        # (Re)infer when there's no map, OR when the loaded map doesn't apply to
        # these records — a stale ./db/role_map.json from a different corpus would
        # otherwise make every record embed the same empty "Title: . Tags:" text,
        # collapsing all vectors to one point (identical, undiscriminating scores).
        record_fields = [r["fields"] for r in new_records]
        if not role_map or not role_map_applies(role_map, record_fields):
            if role_map:
                print("[Builder] loaded role map does not match these records "
                      "(stale/foreign) — re-inferring")
            role_map = infer_roles(record_fields)
            _save_role_map(role_map, args.role_map_file)
            print(f"[Builder] Role map inferred and saved to {args.role_map_file}")

        manifest_num += 1
        print(f"[Builder] Manifest {manifest_num}: {_cid_to_path(mcid)[:16]}... — {len(new_records)} records")
        await _embed_and_upload(args, qdrant, embed_engine, new_records, role_map, dim, truncate, checkpoint)

        _mark_complete(mcid)
        any_new = True
        since_flush += 1
        if since_flush >= CHECKPOINT_EVERY:
            _flush()
            since_flush = 0

        if args.max_manifests and manifest_num >= args.max_manifests:
            print(f"[Builder] Reached --max-manifests={args.max_manifests}; "
                  f"stopping. Re-run to build the next shards (resumes from checkpoint).")
            break

    # Final flush — persist whatever completed since the last batched write.
    if since_flush:
        _flush()

    if not any_new:
        print("\n[Builder] No new bundle manifests to embed.")

    # ── POST-PROCESSING ───────────────────────────────────────────────────────
    if args.umap:
        write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist, tmp_dir=args.umap_tmp_dir, reconnect=_make_qdrant,
                          writeback_batch=args.umap_writeback_batch, writeback_sleep=args.umap_writeback_sleep,
                          writeback_workers=args.umap_writeback_workers,
                          writeback_wait=args.umap_writeback_wait,
                          target=args.umap_target, map_file=args.umap_map_file, role_map=role_map)
    ensure_indexes(qdrant, args.collection)
    print("\n[Builder] All tasks complete.")


if __name__ == "__main__":
    asyncio.run(main())
