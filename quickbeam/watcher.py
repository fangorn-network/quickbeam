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
    build_bundle_joined_data,
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

    # ── Watch-specific ────────────────────────────────────────────────────────
    p.add_argument("--bundle", required=True,
                   help="Bundle schema as name=schemaId (required).")
    p.add_argument("--root-type", default="Track",
                   help="Bundle root node type (default: Track).")

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
async def _poll_once(args, qdrant, embed_engine, role_map_ref,
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

    b_name, b_id = args.bundle.split("=", 1)
    new_count   = 0
    max_block   = last_block

    async for mcid, records in build_bundle_joined_data(
        args, b_id.strip(), args.root_type,
        completed_manifest_cids=completed_manifest_cids,
        owner_filter=owner_filter,
        name_filter=name_filter,
        block_gt=last_block if last_block > 0 else None,
    ):
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

    b_name, b_id = args.bundle.split("=", 1)
    b_name = b_name.strip()

    owner_filter = {o.lower() for o in args.owners}  if args.owners  else None
    name_filter  = {d.lower() for d in args.datasets} if args.datasets else None

    model_dim = MODEL_DIM_MAP.get(args.embedding_model, 768)
    dim       = min(args.dim, model_dim)
    truncate  = dim < model_dim

    print(f"[Watcher] Starting — bundle={b_name!r}")
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
                args, qdrant, embed_engine, role_map_ref,
                dim, truncate, owner_filter, name_filter
            )
            status = f"{new_count} new records embedded" if new_count else "no new records"
            print(f"[Watcher] Cycle {cycle} complete — {status} (last block {last_block})")
        except Exception as e:
            print(f"[Watcher] Cycle {cycle} error: {e}", file=sys.stderr)

        print(f"[Watcher] Sleeping {args.poll_interval}s...")
        await asyncio.sleep(args.poll_interval)


if __name__ == "__main__":
    asyncio.run(main())
