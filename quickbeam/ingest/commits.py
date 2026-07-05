"""The git-native commit layer: resolve on-chain tips to trees, diff commits, and
propagate deletes.

A dataset tip is a commit object (`{tree, parents, embed}`) wrapping the manifest.
This module unwraps tips, and — given the last-built commit — diffs the two trees to
find which entities were removed and tombstones their Qdrant points.
"""
from qdrant_client import models

from quickbeam.ingest.identity import _str_to_uuid, _track_id
from quickbeam.ingest.sources.ipfs import fetch_all_ipfs
from quickbeam.ingest.sources.subgraph import _fetch_all_events_async


async def _unwrap_commit(args, tip_obj, gw_headers):
    """Resolve an on-chain tip to its tree manifest.

    Post-slice-1 the `manifestCid` slot holds a *commit* CID, not a manifest CID
    (the "defer the redeploy" trick). A commit is a small JSON object whose `tree`
    field points at the actual manifest and whose `embed` field carries the
    embedding contract the indexer should inherit (FRAMEWORK Gap A). Older
    publishes still put a raw manifest CID in the slot, so:

      - commit tip  → fetch `commit.tree`, return (manifest, commit)
      - raw manifest → return (obj, None)  [back-compat]

    Returns (manifest_or_None, commit_or_None).
    """
    from quickbeam.objects import is_commit
    if not is_commit(tip_obj):
        return tip_obj, None
    tree = (await fetch_all_ipfs(
        [tip_obj["tree"]], args.ipfs_gateway, args.ipfs_timeout,
        args.concurrency, desc="Commit tree", headers=gw_headers)).get(tip_obj["tree"])
    return tree, tip_obj


def _blob_point_ids(records):
    """Deterministic Qdrant point ids for the entities in one blob's records.

    Mirrors the id derivation at embed time (`_str_to_uuid(_track_id(...))` with
    the node/record id preferred), so the ids computed here for a *removed* blob
    are exactly the points that blob's entities were upserted under."""
    ids = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        ids.append(_str_to_uuid(_track_id(r, prefer=r.get("id"))))
    return ids


def collect_tombstone_ids(removed_uris, blobs_by_uri):
    """Map removed blob uris → point ids to delete. The fan-out/de-dup lives in the
    dep-free `objects.collect_removed_point_ids` (unit-tested there); here we just
    supply the real, embed-time id derivation."""
    from quickbeam.objects import collect_removed_point_ids
    return collect_removed_point_ids(
        removed_uris, blobs_by_uri,
        lambda r: _str_to_uuid(_track_id(r, prefer=r.get("id"))))


async def tombstone_commit_delta(args, qdrant, tip_commit, last_commit_cid, gw_headers):
    """Propagate deletes: remove from the index every entity a new commit dropped
    relative to the last-built commit (slice 2). Diffs the two trees, fetches only
    the *removed* blobs (from the parent) to learn which entities they held, and
    deletes those points. Returns the removed track_ids (so the caller can also
    tombstone the delivered CDN domain — the shards are immutable, so the delete
    must ride the manifest instead).

    A content-addressed no-op (only uris changed) diffs to zero removed blobs, so
    this costs nothing when nothing was actually deleted."""
    from quickbeam.objects import is_commit, plan_delta, collect_removed_point_ids

    if not last_commit_cid or not is_commit(tip_commit):
        return []

    async def _fetch(cids):
        return await fetch_all_ipfs(cids, args.ipfs_gateway, args.ipfs_timeout,
                                    args.concurrency, desc="Delta", headers=gw_headers)

    last_commit = (await _fetch([last_commit_cid])).get(last_commit_cid)
    if not is_commit(last_commit):
        return []
    trees = await _fetch([tip_commit["tree"], last_commit["tree"]])
    child, parent = trees.get(tip_commit["tree"]), trees.get(last_commit["tree"])
    if not child or not parent:
        return []

    plan = plan_delta(child, parent)
    if not plan.removed_uris:
        return []

    removed_blobs = await _fetch(plan.removed_uris)
    # A removed EDGE blob's records are {rel, from, to} triples, not entities —
    # they were never upserted as points, and mapping them through _track_id
    # would mint random ids (junk deletes + junk CDN tombstones). Skip them.
    removed_blobs = {
        uri: [r for r in (records or [])
              if isinstance(r, dict) and not ("rel" in r and "from" in r and "to" in r)]
        for uri, records in removed_blobs.items()
    }
    # Same fan-out/de-dup as the point ids, keyed on the record's track_id; the
    # point id is a deterministic uuid5 of it (see _blob_point_ids).
    track_ids = collect_removed_point_ids(
        plan.removed_uris, removed_blobs,
        lambda r: _track_id(r, prefer=r.get("id")))
    if track_ids:
        qdrant.delete(collection_name=args.collection,
                      points_selector=models.PointIdsList(
                          points=[_str_to_uuid(t) for t in track_ids]),
                      wait=True)
        print(f"[Builder] tombstoned {len(track_ids)} point(s) from "
              f"{len(plan.removed_uris)} removed blob(s)")
    return track_ids


async def resolve_tip_commit(args, schema_id, gw_headers, owner_filter=None, name_filter=None):
    """Resolve the current on-chain tip for a schema → (tip_cid, commit, manifest).

    ``commit`` is None for a legacy raw-manifest tip. Used by the watcher to (a)
    inherit the embedding contract from the tip commit at startup (Gap A) and (b)
    diff the new tip against the last-built one for delete propagation."""
    publishes, updates = await _fetch_all_events_async(
        args.subgraph_url, args.graph_api_key, schema_id, args.page_size)
    events = publishes + updates
    if owner_filter:
        events = [e for e in events if e.get("owner", "").lower() in owner_filter]
    if name_filter:
        events = [e for e in events if e.get("name", "").lower() in name_filter]
    if not events:
        return None, None, None
    ev = max(events, key=lambda e: int(e.get("blockNumber", 0)))
    tip_cid = ev["manifestCid"]
    obj = (await fetch_all_ipfs(
        [tip_cid], args.ipfs_gateway, args.ipfs_timeout,
        args.concurrency, desc="Tip", headers=gw_headers)).get(tip_cid)
    manifest, commit = await _unwrap_commit(args, obj, gw_headers) if obj else (None, None)
    return tip_cid, commit, manifest
