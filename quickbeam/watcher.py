"""
quickbeam watch — live embedding daemon.

Polls the subgraph for new ManifestPublished / ManifestUpdated events and
automatically embeds new records into Qdrant as they arrive.

Filter hierarchy (all optional, combinable):
  --bundle name=id            required — which bundle schema to watch
  --owner  0x...              filter to one or more publisher addresses
  --dataset "Track" "Audio"   filter to one or more dataset names (the 'name'
                              field on the ManifestPublished event)

Examples
--------
  # Watch everything in the bundle:
  quickbeam watch --bundle fangorn=0xabc...

  # Only a specific publisher:
  quickbeam watch --bundle fangorn=0xabc... --owner 0xdeadbeef

  # Only certain dataset names (any publisher):
  quickbeam watch --bundle fangorn=0xabc... --dataset Track Recording

  # Most specific — one publisher's named datasets:
  quickbeam watch --bundle fangorn=0xabc... --owner 0xdeadbeef --dataset Track
"""

import argparse
import asyncio
import json
import os
import sys

from quickbeam.embeddings import (
    MODEL_DIM_MAP,
    _load_checkpoint,
    _save_checkpoint,
    _save_role_map,
    _init_embed_engine,
    _load_profiles,
    build_bundle_joined_data,
    build_view_joined_data,
    _embed_and_upload,
    ensure_indexes,
    matryoshka,
    write_umap_coords,
)
from qdrant_client import QdrantClient
from qdrant_client import models
from quickbeam.roles import infer_roles


# ---------------------------------------------------------------------------
# ARG PARSING
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="Quickbeam Watch — live embedding daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── What to watch ─────────────────────────────────────────────────────────
    # Exactly one of --bundle / --view. A bundle is one publisher's graph (edge-walk
    # join). A view fuses several sources + linksets into one graph (union-find on
    # global identity) — the only path that honors linksets. See embeddings.py.
    p.add_argument("--bundle", default=None,
                   help="Bundle schema as name=schemaId (single publisher's graph).")
    p.add_argument("--view", default=None,
                   help="View schema as name=schemaId (fuses sources + linksets).")

    # Projection profiles — how graph roots become documents. Mirrors `build`.
    p.add_argument("--root-type", default="Track",
                   help="Root node type when no --root-profile is given (default: Track).")
    p.add_argument("--root-profile", action="append", default=[],
                   help="Named projection(s) to emit, repeatable. Falls back to a single "
                        "--root-type projection when omitted.")
    p.add_argument("--profiles-file", default=None,
                   help="Optional JSON file of custom/override root profiles.")
    p.add_argument("--max-depth", type=int, default=2, help="Graph-walk depth per profile.")
    p.add_argument("--label-cap", type=int, default=50, help="Max folded labels per group.")
    p.add_argument("--node-cap", type=int, default=2000, help="Max nodes visited per root.")

    # Filter hierarchy
    g = p.add_argument_group("filter hierarchy (all optional, combinable)")
    g.add_argument("--owner", action="append", default=[], dest="owners",
                   metavar="ADDRESS",
                   help="Only process events from this publisher. Repeatable.")
    g.add_argument("--dataset", nargs="+", default=[], dest="datasets",
                   metavar="NAME",
                   help="Only process events whose dataset name matches. "
                        "Pass multiple names to accept any of them.")

    p.add_argument("--poll-interval", type=int, default=60,
                   help="Seconds between subgraph polls (default: 60).")

    # ── Live CDN delivery ─────────────────────────────────────────────────────
    # When set, after any cycle that embeds new records the watcher writes them as a
    # delta shard into the baked CDN domain (see cdn.append_domain). This closes the
    # loop: on-chain publish → embed → deliver, shipping only the delta (no re-bake).
    g2 = p.add_argument_group("live CDN delivery (optional)")
    g2.add_argument("--cdn-dir", default=None,
                    help="Baked CDN directory. Enables live delta delivery when set.")
    g2.add_argument("--cdn-domain", default=None,
                    help="Domain to append new records to (must already be baked).")
    g2.add_argument("--cdn-config", default="domains.json",
                    help="Domain config used to resolve the append scan filter.")

    # ── Shared with build ─────────────────────────────────────────────────────
    p.add_argument("--subgraph-url",
                   default="https://gateway.thegraph.com/api/subgraphs/id/8SgbhtiitpAhEfyTgeAHxHH5DQ2gTygUuXgc3b7MCFyc")
    p.add_argument("--graph-api-key", default="")
    p.add_argument("--ipfs-gateway", default="https://gateway.pinata.cloud/ipfs")
    p.add_argument("--ipfs-gateway-key", default=None)
    p.add_argument("--qdrant-host", default="localhost")
    p.add_argument("--qdrant-port", type=int, default=6333)
    p.add_argument("--qdrant-grpc-port", type=int, default=6334)
    p.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    p.add_argument("--collection", default="fangorn")
    p.add_argument("--searchable-fields", default="auto")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--ipfs-timeout", type=int, default=20)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--embedding-model", default="nomic-ai/nomic-embed-text-v1.5")
    p.add_argument("--dim", type=int, default=256)
    p.add_argument("--embed-batch", type=int, default=16)
    p.add_argument("--role-map-file", default="./db/role_map.json")

    return p.parse_args()


# ---------------------------------------------------------------------------
# SINGLE POLL CYCLE
# ---------------------------------------------------------------------------
async def _poll_once(args, qdrant, embed_engine, role_map_ref, profiles,
                     dim, truncate, owner_filter, name_filter):
    """
    Run one poll cycle. Returns (new_count, last_block_seen).

    role_map_ref is a one-element list so we can update it in place across
    cycles without needing a nonlocal or class.
    """
    checkpoint = _load_checkpoint(args.checkpoint_file)
    completed_manifest_cids = set(checkpoint["completed_manifest_cids"])
    processed_track_ids     = set(checkpoint["processed_track_ids"])
    # last_block: highest blockNumber seen so far, used as block_gt on the
    # next query so we only fetch truly new events.
    last_block = checkpoint.get("last_block", 0)

    _, schema_id = (args.view or args.bundle).split("=", 1)
    new_count   = 0
    max_block   = last_block

    # View mode fuses sources + linksets, keyed on the view manifest CID; it does not
    # take per-event owner/name/block filters (fusion is inherently cross-source).
    if args.view:
        data_gen = build_view_joined_data(
            args, schema_id.strip(), profiles,
            completed_manifest_cids=completed_manifest_cids,
        )
    else:
        data_gen = build_bundle_joined_data(
            args, schema_id.strip(), profiles,
            completed_manifest_cids=completed_manifest_cids,
            owner_filter=owner_filter,
            name_filter=name_filter,
            block_gt=last_block if last_block > 0 else None,
        )

    async for mcid, records in data_gen:
        new_records = [r for r in records if r["track_id"] not in processed_track_ids]

        if not new_records:
            completed_manifest_cids.add(mcid)
            checkpoint["completed_manifest_cids"] = list(completed_manifest_cids)
            checkpoint["processed_track_ids"] = []
            processed_track_ids.clear()
            _save_checkpoint(checkpoint, args.checkpoint_file)
            continue

        # Infer role map from first real batch if none loaded yet.
        if not role_map_ref[0]:
            role_map_ref[0] = infer_roles([r["fields"] for r in new_records])
            _save_role_map(role_map_ref[0], args.role_map_file)
            print(f"[Watcher] Role map inferred and saved to {args.role_map_file}")

        owner = records[0]["meta"].get("owner", "?")[:10]
        name  = records[0]["meta"].get("name", "?")
        print(f"[Watcher] New manifest {name!r} from {owner}... — {len(new_records)} records")

        await _embed_and_upload(
            args, qdrant, embed_engine, new_records, role_map_ref[0],
            dim, truncate, checkpoint
        )

        # Track the highest block number seen this cycle for the next poll's block_gt.
        blk = int(records[0]["meta"].get("blockNumber", 0))
        max_block = max(max_block, blk)

        completed_manifest_cids.add(mcid)
        checkpoint["completed_manifest_cids"] = list(completed_manifest_cids)
        checkpoint["processed_track_ids"] = []
        processed_track_ids.clear()
        _save_checkpoint(checkpoint, args.checkpoint_file)
        new_count += len(new_records)

    if max_block > last_block:
        checkpoint = _load_checkpoint(args.checkpoint_file)
        checkpoint["last_block"] = max_block
        _save_checkpoint(checkpoint, args.checkpoint_file)

    return new_count, max_block


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
async def main():
    args = parse_args()

    if bool(args.bundle) == bool(args.view):
        sys.exit("[Watcher] pass exactly one of --bundle or --view.")

    b_name, _ = (args.view or args.bundle).split("=", 1)
    b_name = b_name.strip()
    mode = "view" if args.view else "bundle"

    # Resolve projection profiles once (same as `build`). These drive how graph roots
    # become documents; required by both the bundle and view join paths.
    profiles = _load_profiles(args)

    owner_filter = {o.lower() for o in args.owners}  if args.owners  else None
    name_filter  = {d.lower() for d in args.datasets} if args.datasets else None

    model_dim = MODEL_DIM_MAP.get(args.embedding_model, 768)
    dim       = min(args.dim, model_dim)
    truncate  = dim < model_dim

    prof_desc = ", ".join("{}->{}".format(p["name"], p["root_type"]) for p in profiles)
    print(f"[Watcher] Starting — {mode}={b_name!r}")
    print(f"[Watcher] profiles: {prof_desc}")
    if args.bundle:
        print(f"[Watcher] owners  : {', '.join(args.owners)  or 'any'}")
        print(f"[Watcher] datasets: {', '.join(args.datasets) or 'any'}")
    print(f"[Watcher] poll interval: {args.poll_interval}s")

    qdrant = QdrantClient(
        host=args.qdrant_host, port=args.qdrant_port,
        grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=600
    )

    if not qdrant.collection_exists(args.collection):
        qdrant.create_collection(
            args.collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE, on_disk=True)
        )
    ensure_indexes(qdrant, args.collection)

    # Init embed engine once and keep it alive across cycles.
    embed_engine = _init_embed_engine(args)
    print(f"[Watcher] model dim={model_dim}, output dim={dim} (truncate={truncate})")

    # Load existing role map; inferred on first cycle if absent.
    role_map_ref = [{}]
    if os.path.exists(args.role_map_file):
        with open(args.role_map_file) as f:
            role_map_ref[0] = json.load(f)

    cycle = 0
    while True:
        cycle += 1
        print(f"\n[Watcher] ── cycle {cycle} ──────────────────────────────")
        try:
            new_count, last_block = await _poll_once(
                args, qdrant, embed_engine, role_map_ref, profiles,
                dim, truncate, owner_filter, name_filter
            )
            status = f"{new_count} new records embedded" if new_count else "no new records"
            print(f"[Watcher] Cycle {cycle} complete — {status} (last block {last_block})")

            # Live CDN delivery: ship the just-embedded points as a delta shard so
            # clients pull only the delta (no full re-bake). Runs off the same Qdrant
            # client; a failure here never kills the watch loop.
            if new_count and args.cdn_dir and args.cdn_domain:
                try:
                    from quickbeam.cdn import append_domain
                    append_domain(qdrant, args.collection, args.cdn_dir,
                                  args.cdn_domain, config_path=args.cdn_config)
                except Exception as e:  # noqa: BLE001
                    print(f"[Watcher] CDN append error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[Watcher] Cycle {cycle} error: {e}", file=sys.stderr)

        print(f"[Watcher] Sleeping {args.poll_interval}s...")
        await asyncio.sleep(args.poll_interval)


if __name__ == "__main__":
    asyncio.run(main())
