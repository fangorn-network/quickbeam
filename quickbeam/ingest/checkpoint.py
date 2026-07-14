"""Resumable-build state: the ingest checkpoint and the role-map sidecar.

The checkpoint is the single JSON file (`--checkpoint-file`) that makes `build` and
`watch` resumable in the owner:namespace data model. It records:

  * `processed_track_ids` — every vertex CID already embedded, so a re-seed or the
    seed↔live overlap on a `fangorn subscribe` reconnect never double-embeds (point
    ids are deterministic, so this is dedupe-for-work-avoidance, not correctness).
  * `sources` — per `"owner:namespace"` state: the last on-chain `head` (root) seen
    (to skip a cycle with no change) and the `vertex_cids` present at that head (to
    diff against the next read and tombstone vertices that dropped out).
"""
import json
import os


def _load_checkpoint(path):
    try:
        with open(path) as f:
            ck = json.load(f)
            ck.setdefault("processed_track_ids", [])
            # sources: "owner:namespace" -> {"head": "0x...", "vertex_cids": [...]}
            ck.setdefault("sources", {})
            return ck
    except Exception:
        return {"processed_track_ids": [], "sources": {}}


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
