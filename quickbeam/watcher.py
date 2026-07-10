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
    resolve_tip_commit,
    tombstone_commit_delta,
    write_umap_coords,
)
from quickbeam.objects import resolve_embed
from qdrant_client import QdrantClient
from qdrant_client import models
from quickbeam.roles import infer_roles, role_map_applies


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
    p.add_argument("--root-profile", action="append", default=[],
                   help="Named projection(s) to emit, repeatable (required). "
                        "e.g. --root-profile asset --root-profile transfer.")
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
                   default="https://gateway.thegraph.com/api/subgraphs/id/2yVbpC7TT1VPq9vLn8a49zCjESNAEjoPg8wZhriQDDcY")
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
                     dim, truncate, owner_filter, name_filter, edges_sink=None,
                     tombstones_sink=None):
    """
    Run one poll cycle. Returns (new_count, last_block_seen).

    role_map_ref is a one-element list so we can update it in place across
    cycles without needing a nonlocal or class. edges_sink, when provided, collects
    the typed edges fetched this cycle so the caller can ship them to the CDN's
    relational axis (bundle mode only; view mode fuses linksets differently).
    tombstones_sink collects the track_ids delete-propagation removed this cycle so
    the caller can mirror the deletes into the delivered CDN domain.
    """
    checkpoint = _load_checkpoint(args.checkpoint_file)
    completed_manifest_cids = set(checkpoint["completed_manifest_cids"])
    processed_track_ids     = set(checkpoint["processed_track_ids"])

    _, schema_id = (args.view or args.bundle).split("=", 1)
    schema_id = schema_id.strip()

    # last_block: highest blockNumber seen so far *for this schema*, used as
    # block_gt on the next query so we only fetch truly new events. Keyed per
    # schema_id — otherwise pointing --bundle/--view at a different (or rebuilt)
    # schema would inherit a stale, unrelated cursor and silently filter out
    # every one of its events.
    last_block = checkpoint.get("last_block", {}).get(schema_id, 0)
    new_count   = 0
    max_block   = last_block

    # Delete propagation (slice 2): before building, diff the current tip commit
    # against the last one we built and tombstone any entities it dropped. Bundle
    # mode only — view-tip diffing arrives with merge commits (slice 4). Keyed on
# schema here (one repo per schema); multi-dataset repos refine this later.
    if args.bundle:
        gw_headers = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}
        last_tip = checkpoint.get("last_tip", {}).get(schema_id)
        try:
            tip_cid, tip_commit, _ = await resolve_tip_commit(
                args, schema_id, gw_headers, owner_filter, name_filter)
            if tip_commit and tip_cid != last_tip:
                removed = await tombstone_commit_delta(args, qdrant, tip_commit, last_tip, gw_headers)
                if removed:
                    print(f"[Watcher] delete-propagation: removed {len(removed)} point(s)")
                    if tombstones_sink is not None:
                        tombstones_sink.extend(removed)
                checkpoint.setdefault("last_tip", {})[schema_id] = tip_cid
                _save_checkpoint(checkpoint, args.checkpoint_file)
        except Exception as e:  # noqa: BLE001
            print(f"[Watcher] tombstone step skipped: {e}", file=sys.stderr)

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
            edges_sink=edges_sink,
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

        # Infer the role map from the first real batch if none is loaded yet, OR if
        # the loaded map doesn't apply to these records — a stale ./db/role_map.json
        # from a different corpus would otherwise make every record embed the same
        # empty "Title: . Tags:" text, collapsing all vectors to one point.
        rec_fields = [r["fields"] for r in new_records]
        if not role_map_ref[0] or not role_map_applies(role_map_ref[0], rec_fields):
            if role_map_ref[0]:
                print("[Watcher] loaded role map does not match these records "
                      "(stale/foreign) — re-inferring")
            role_map_ref[0] = infer_roles(rec_fields)
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
        checkpoint.setdefault("last_block", {})[schema_id] = max_block
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

    # Inherit the embedding contract from the tip commit (FRAMEWORK Gap A): the
    # model / dim / distance the index is sized to come from the *data's* commit,
    # not a hardcoded CLI default. Falls back to the flags when the tip carries no
    # embed contract (or is a legacy raw-manifest tip).
    embed_distance = "Cosine"
    gw_headers = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}
    _, schema_id0 = (args.view or args.bundle).split("=", 1)
    try:
        _, tip_commit0, _ = await resolve_tip_commit(args, schema_id0.strip(), gw_headers)
        if tip_commit0 and tip_commit0.get("embed"):
            embed = resolve_embed(tip_commit0, args.embedding_model, args.dim)
            print(f"[Watcher] inheriting embed contract from tip commit: "
                  f"model={embed['model']} dim={embed['dim']} distance={embed['distance']}")
            args.embedding_model = embed["model"]
            args.dim = embed["dim"]
            embed_distance = embed["distance"]
    except Exception as e:  # noqa: BLE001
        print(f"[Watcher] embed-contract resolve skipped: {e}", file=sys.stderr)

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
        _d = str(embed_distance).lower()
        _dist = (models.Distance.DOT if _d.startswith("dot")
                 else models.Distance.EUCLID if _d.startswith("eucl")
                 else models.Distance.COSINE)
        qdrant.create_collection(
            args.collection,
            vectors_config=models.VectorParams(size=dim, distance=_dist, on_disk=True)
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

    # Bootstrap live CDN delivery. The per-cycle delta path (append_domain) can only
    # EXTEND an already-baked domain, so if a delivery target is set but not yet baked,
    # bake it once now from whatever the collection already holds. This makes `cdn serve`
    # start immediately (no manual `cdn bake`) and gives append_domain the base manifest
    # it grows each cycle. Domains need no domains.json entry — a missing spec bakes all.
    if args.cdn_dir and args.cdn_domain:
        manifest_path = os.path.join(args.cdn_dir, args.cdn_domain, "manifest.json")
        if os.path.exists(manifest_path):
            print(f"[Watcher] CDN domain {args.cdn_domain!r} already baked — deltas append per cycle")
        else:
            print(f"[Watcher] CDN domain {args.cdn_domain!r} not baked — baking initial snapshot...")
            try:
                from quickbeam.cdn import bake_domain
                entry = bake_domain(qdrant, args.collection, args.cdn_dir, args.cdn_domain,
                                    config_path=args.cdn_config, model=args.embedding_model)
                print(f"[Watcher] initial CDN bake: {entry['count']} point(s) into "
                      f"{args.cdn_dir}/{args.cdn_domain}")
            except Exception as e:  # noqa: BLE001
                print(f"[Watcher] initial CDN bake failed: {e}", file=sys.stderr)

    cycle = 0
    while True:
        cycle += 1
        print(f"\n[Watcher] ── cycle {cycle} ──────────────────────────────")
        try:
            cycle_edges: list = []
            cycle_tombstones: list = []
            new_count, last_block = await _poll_once(
                args, qdrant, embed_engine, role_map_ref, profiles,
                dim, truncate, owner_filter, name_filter, edges_sink=cycle_edges,
                tombstones_sink=cycle_tombstones
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

            # Mirror this cycle's delete propagation into the delivered domain:
            # shards are immutable, so removals ride the manifest's tombstones
            # list (clients drop those rows at load; dead edges are pruned).
            # The blob-level diff over-reports: an entity in a *changed* blob is
            # "removed" then re-embedded the same cycle — so only ids that are
            # actually absent from the index after the cycle are tombstoned.
            if cycle_tombstones and args.cdn_dir and args.cdn_domain:
                try:
                    from quickbeam.cdn import append_tombstones
                    from quickbeam.embeddings import _str_to_uuid
                    by_uuid = {_str_to_uuid(t): t for t in set(cycle_tombstones)}
                    found = qdrant.retrieve(args.collection, ids=list(by_uuid),
                                            with_payload=False, with_vectors=False)
                    alive = {str(pt.id) for pt in found}
                    dead = [t for u, t in by_uuid.items() if u not in alive]
                    if dead:
                        append_tombstones(args.cdn_dir, args.cdn_domain, dead)
                except Exception as e:  # noqa: BLE001
                    print(f"[Watcher] CDN tombstone error: {e}", file=sys.stderr)

            # Relational axis: merge this cycle's typed edges into the served
            # edges.json so `neighbors` grows with the stream (dedup + incremental,
            # mirroring the shard delta). Isolated so a failure never kills the loop.
            if cycle_edges and args.cdn_dir and args.cdn_domain:
                try:
                    from quickbeam.cdn import append_edges
                    added = append_edges(args.cdn_dir, args.cdn_domain, cycle_edges)
                    if added:
                        print(f"[Watcher] CDN edges: +{added['added']} new "
                              f"({added['count']} total; relations={added['relations']})")
                except Exception as e:  # noqa: BLE001
                    print(f"[Watcher] CDN edges error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[Watcher] Cycle {cycle} error: {e}", file=sys.stderr)

        print(f"[Watcher] Sleeping {args.poll_interval}s...")
        await asyncio.sleep(args.poll_interval)


if __name__ == "__main__":
    asyncio.run(main())
