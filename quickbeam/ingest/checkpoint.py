"""Resumable-build state: the ingest checkpoint and the role-map sidecar.

The checkpoint is the single JSON file (`--checkpoint-file`) that makes `build` and
`watch` resumable — it records which manifests are fully embedded, the in-flight
manifest's records (for mid-manifest crash recovery), and per-schema last-built tips.
"""
import json
import os


def _load_checkpoint(path):
    try:
        with open(path) as f:
            ck = json.load(f)
            ck.setdefault("manifests", {})
            ck.setdefault("completed_manifest_cids", [])
            ck.setdefault("processed_track_ids", [])
            # last_tip: schemaId -> last-built tip (commit) CID, for commit-diff
            # delete propagation across cycles (slice 2).
            ck.setdefault("last_tip", {})
            # last_block: schemaId -> highest blockNumber seen, for incremental
            # polling. Was a single global scalar pre-fix, which leaked a stale
            # cursor across schemas: watching a new (or rebuilt) subgraph would
            # inherit an unrelated schema's block height and filter out every one
            # of its events via blockNumber_gt. A legacy scalar can't be attributed
            # to any one schema, so it's dropped here rather than misapplied.
            if not isinstance(ck.get("last_block"), dict):
                ck["last_block"] = {}
            return ck
    except Exception:
        return {"manifests": {}, "processed_track_ids": [],
                "completed_manifest_cids": [], "last_tip": {}, "last_block": {}}


def _save_checkpoint(ck, path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(ck, f)


def _save_role_map(role_map, path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(role_map, f)
