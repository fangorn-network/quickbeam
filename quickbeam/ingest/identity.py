"""Deterministic identity + the vector transform.

Two orthogonal-but-related primitives that everything downstream depends on:

  * `_track_id` / `_str_to_uuid` — how a record becomes a stable Qdrant point id.
    Determinism is what makes re-upserting a manifest idempotent (a crash re-runs
    overwrite the same points instead of duplicating them).
  * `matryoshka` — the LayerNorm → slice-to-dim → L2-normalize transform applied to
    every document embedding. The pull-client reuses THIS function on the query side
    so query and document vectors share one space; keep it the single source of truth.
"""
import hashlib
import uuid

import numpy as np


def _str_to_uuid(s: str) -> str:
    """Deterministic UUID v5 from a track id. Using a stable id (rather than a
    random one) makes re-upserting a manifest idempotent — a crash that re-runs
    an already-embedded manifest overwrites the same points instead of creating
    duplicates. Matches server.py's _str_to_uuid so builder + bundle-import agree.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, s))


def _track_id(fields: dict, prefer: str | None = None) -> str:
    if prefer:
        return str(prefer).strip().removeprefix("track:")
    for key in ["trackId", "track_id", "id", "contentId"]:
        if fields.get(key): return str(fields[key]).strip().removeprefix("track:")
    artist = str(fields.get("artist") or "").strip()
    title  = str(fields.get("title")  or "").strip()
    if artist and title: return hashlib.sha256(f"{artist}:{title}".encode()).hexdigest()[:24]
    return str(uuid.uuid4())[:12]


def matryoshka(vec, dim):
    x = np.asarray(vec, dtype=np.float32)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-5)
    x = x[:dim]
    n = np.linalg.norm(x)
    return (x / n).astype(np.float32).tolist() if n else x.tolist()
