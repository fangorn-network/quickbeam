"""The `quickbeam build` CLI — the offline, one-shot ingestion driver.

Reads one or more owner:namespace sources off-chain (`fangorn read`), projects each
namespace's `{vertices, edges}` graph into root-profile documents, embeds them into
Qdrant, and optionally projects a 2-D UMAP map afterwards. The live counterpart is
`watcher.py`, which reuses the same ingest primitives over a `fangorn subscribe` stream.
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
from quickbeam.ingest.umap import write_umap_coords
from quickbeam.ingest.graph.projection import load_profiles, project_source
from quickbeam.ingest.sources.fangorn import parse_sources, read_source


# ---------------------------------------------------------------------------
# CONFIG & ARGS
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Fangorn Qdrant Ingestion CLI Builder")
    parser.add_argument("--source", action="append", default=[], dest="sources",
                        metavar="OWNER:NAMESPACE",
                        help="Namespace to embed, as OWNER:NAMESPACE. Repeatable. Each is "
                             "read off-chain via `fangorn read` and projected into documents.")
    parser.add_argument("--fangorn-bin", default="fangorn",
                        help="The `fangorn` CLI invocation (shell-split, so a full command "
                             "works, e.g. the dev wrapper 'node .../cli.js').")
    parser.add_argument(
        "--root-profile",
        action="append",
        default=[],
        help="Named projection(s) to emit, repeatable: e.g. --root-profile track "
             "--root-profile place. Each name is taken literally as a root vertex tag; "
             "the profile walks the graph from vertices carrying that tag and emits a "
             "distinct document. With none given, one profile is auto-derived per "
             "distinct vertex tag in the source.",
    )
    parser.add_argument("--profiles-file", default=None,
                        help="Optional JSON file of custom/override root profiles, "
                             "merged over the auto-derived per-tag defaults.")
    parser.add_argument("--max-depth", type=int, default=2,
                        help="Default traversal depth for profiles that don't set one.")
    parser.add_argument("--label-cap", type=int, default=50,
                        help="Max neighbor labels collected per relation group in a projection.")
    parser.add_argument("--node-cap", type=int, default=2000,
                        help="Max nodes a single root's graph walk will visit (cost bound).")

    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--qdrant-grpc-port", type=int, default=6334)
    parser.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    parser.add_argument("--collection", default="fangorn")
    parser.add_argument("--searchable-fields", default="auto")
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

    checkpoint = _load_checkpoint(args.checkpoint_file)
    processed_track_ids = set(checkpoint["processed_track_ids"])

    sources = parse_sources(args.sources)
    if not sources:
        print("[Builder] Nothing to do — pass --source OWNER:NAMESPACE (repeatable), "
              "or --umap-only to (re)project an existing collection.")
        return

    if args.reset and qdrant.collection_exists(args.collection):
        print(f"[Builder] Resetting collection '{args.collection}'...")
        qdrant.delete_collection(args.collection)
        processed_track_ids.clear()
        checkpoint["processed_track_ids"] = []
        checkpoint["sources"] = {}

    role_map: dict = {}
    if os.path.exists(args.role_map_file):
        with open(args.role_map_file) as f:
            role_map = json.load(f)

    # Lazy-init: don't load the model into GPU VRAM until we know there's
    # actual new work to do (avoids OOM when everything is already embedded).
    embed_engine = None
    any_new = False

    for owner, namespace in sources:
        key = f"{owner}:{namespace}"
        print(f"\n[Builder] Reading {key} via `fangorn read`...")
        contents = read_source(args.fangorn_bin, owner, namespace)
        discovered_tags = {v["schemaId"] for v in contents.get("vertices", [])}
        profiles = load_profiles(args, discovered_tags)
        _prof_desc = ", ".join(f"{p['name']}→{p['root_type']}" for p in profiles)
        print(f"[Builder] {key} — {len(contents.get('vertices', []))} vertices, "
              f"{len(contents.get('edges', []))} edges — projections: {_prof_desc}")

        records = project_source(owner, namespace, contents, profiles, args)
        curr_ids = {r["track_id"] for r in records}
        new_records = [r for r in records if r["track_id"] not in processed_track_ids]

        # Tombstone roots that were present at the last build of this source but
        # dropped out of the current read (deleted upstream) — mirror the watcher,
        # so a rebuild converges the collection to exactly the namespace's roots.
        src_ck = checkpoint.setdefault("sources", {}).setdefault(key, {})
        prev_ids = set(src_ck.get("vertex_cids", []))
        dropped = prev_ids - curr_ids
        if dropped and qdrant.collection_exists(args.collection):
            from quickbeam.ingest.identity import _str_to_uuid
            qdrant.delete(
                collection_name=args.collection,
                points_selector=models.PointIdsList(
                    points=[_str_to_uuid(cid) for cid in dropped]))
            processed_track_ids -= dropped
            print(f"[Builder] {key} — tombstoned {len(dropped)} dropped root(s).")

        src_ck["head"] = contents.get("head")
        src_ck["vertex_cids"] = list(curr_ids)

        if not new_records:
            print(f"[Builder] {key} — nothing new to embed.")
            checkpoint["processed_track_ids"] = list(processed_track_ids)
            _save_checkpoint(checkpoint, args.checkpoint_file)
            continue

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

        print(f"[Builder] {key} — embedding {len(new_records)} new records...")
        await _embed_and_upload(args, qdrant, embed_engine, new_records, role_map, dim, truncate, checkpoint)

        processed_track_ids.update(r["track_id"] for r in new_records)
        checkpoint["processed_track_ids"] = list(processed_track_ids)
        _save_checkpoint(checkpoint, args.checkpoint_file)
        any_new = True

    if not any_new:
        print("\n[Builder] No new records to embed.")

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
