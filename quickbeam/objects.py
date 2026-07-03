"""
quickbeam/objects.py — the git-native object model, Python side.

Mirror of the TypeScript SDK's ``src/objects/`` (see ``fangorn/docs/objects/``).
Fangorn data is a Merkle DAG in IPFS, exactly like git stores code:

    blob   — an immutable chunk of records/nodes/edges, named by the hash of its
             bytes (the chunk CIDs the publisher uploads).
    tree   — a snapshot: the set of blobs that make up the dataset at one moment,
             plus one Poseidon2 root. In v1 an existing *manifest* (record-set /
             bundle / view / linkset) already IS the tree — we wrap it, not
             reinvent it.
    commit — a tree plus provenance: parents, author, timestamp, schema, and the
             optional embedding contract the indexer inherits (FRAMEWORK Gap A).
    ref    — the mutable on-chain pointer to the tip commit. Today that's the
             ``manifest_cid`` slot in the DataSource registry; a commit CID rides
             in it unchanged (the "defer the redeploy" trick, plan slice 1).

quickbeam only ever *reads* commits (it resolves the on-chain tip and walks
history to build the delta), so this module parses + diffs. ``canonicalize`` is
included for cross-language conformance and for when the indexer starts *writing*
commits (index-as-a-repo, slice 7); its output is asserted byte-for-byte against
the shared golden fixture.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Iterable

COMMIT_KIND = "commit"


# ---------------------------------------------------------------------------
# CANONICAL SERIALIZATION
#
# A commit's CID must be a pure function of its logical contents — the *same*
# commit built in TypeScript and Python has to hash to the same bytes. Plain
# json.dumps preserves insertion order and escapes non-ASCII, both of which
# diverge from the TS canonicalizer, so we roll our own to match it exactly:
#
#   - object keys sorted lexicographically, recursively
#   - no insignificant whitespace
#   - None-valued keys dropped (so optional fields never perturb the hash)
#   - non-ASCII kept literal (UTF-8), matching JS JSON.stringify
#   - bytes encoded as {"__type":"Uint8Array","data":<base64>}
# ---------------------------------------------------------------------------
def canonicalize(value: Any) -> str:
    """Return the canonical JSON string for a content-addressed object."""
    return _encode(value)


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("cannot canonicalize non-finite number")
        # json.dumps renders floats the same way JS Number->string does for the
        # finite values we carry (dim is an int; floats are rare in objects).
        return json.dumps(value)
    if isinstance(value, str):
        # ensure_ascii=False keeps non-ASCII literal, matching JS JSON.stringify.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (bytes, bytearray)):
        return _encode({"__type": "Uint8Array",
                         "data": base64.b64encode(bytes(value)).decode("ascii")})
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(k for k in value if value[k] is not None)
        body = ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + _encode(value[k]) for k in keys
        )
        return "{" + body + "}"
    raise TypeError(f"cannot canonicalize value of type {type(value).__name__}")


# ---------------------------------------------------------------------------
# COMMIT
# ---------------------------------------------------------------------------
def is_commit(obj: Any) -> bool:
    """Loose structural check — the discriminant plus the load-bearing fields.
    Kept lenient (like the TS ``isCommit``) so a future field addition doesn't
    reject older commits when walking history."""
    return (
        isinstance(obj, dict)
        and obj.get("kind") == COMMIT_KIND
        and isinstance(obj.get("parents"), list)
        and isinstance(obj.get("tree"), str)
        and isinstance(obj.get("root"), str)
        and isinstance(obj.get("schemaId"), str)
        and isinstance(obj.get("author"), str)
        and isinstance(obj.get("message"), str)
        and isinstance(obj.get("timestamp"), (int, float))
    )


def first_parent(commit: dict) -> str | None:
    """The mainline parent CID, or None for a root commit."""
    parents = commit.get("parents") or []
    return parents[0] if parents else None


def resolve_embed(commit: dict | None, fallback_model: str, fallback_dim: int,
                  fallback_distance: str = "Cosine") -> dict:
    """The embedding contract the indexer should use: read it from the commit
    (FRAMEWORK Gap A — model/dim/distance inherited from the data, not hardcoded)
    and fall back to the caller's CLI flags only for whatever the commit omits."""
    embed = (commit or {}).get("embed") or {}
    return {
        "model": embed.get("model") or fallback_model,
        "dim": int(embed.get("dim") or fallback_dim),
        "distance": embed.get("distance") or fallback_distance,
    }


# ---------------------------------------------------------------------------
# TREE (manifest) — blob extraction + diff
# ---------------------------------------------------------------------------
class BlobRef:
    """One blob leaf: its stable content identity and its retrieval URI.

    content_id — sha256 of the serialized chunk bytes. STABLE across commits and
                 CARs, so byte-identical chunks share it. This is what we diff and
                 dedup on. (Falls back to the uri for pre-contentId manifests.)
    uri        — the ``ipfs://<carRoot>/<entry>`` path used to actually fetch it.
                 NOT stable across commits, so it must never be the diff key.
    """

    __slots__ = ("content_id", "uri")

    def __init__(self, content_id: str, uri: str):
        self.content_id = content_id
        self.uri = uri

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"BlobRef(content_id={self.content_id!r}, uri={self.uri!r})"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, BlobRef)
                and other.content_id == self.content_id and other.uri == self.uri)


def _ref(uri: Any, content_id: Any) -> BlobRef | None:
    if not isinstance(uri, str):
        return None
    # pre-contentId manifests diff coarsely on the uri (their old behavior).
    return BlobRef(content_id if isinstance(content_id, str) else uri, uri)


def blob_refs(manifest: dict) -> list[BlobRef]:
    """Pull every blob leaf (identity + retrieval uri) from a manifest, any kind.
    Mirrors ``blobRefs`` in the TS store, including the legacy single-``edgeChunk``
    bundle shape the builder path already tolerates."""
    kind = manifest.get("kind")
    refs: list[BlobRef | None] = []
    if kind == "record-set":
        for e in manifest.get("entries", []):
            fields = e.get("fields") or {}
            refs.append(_ref(fields.get("dataCid"), fields.get("contentId")))
    elif kind == "bundle" or manifest.get("version") == 3:
        for c in manifest.get("nodeChunks", []):
            refs.append(_ref(c.get("dataCid"), c.get("contentId")))
        edge_chunks = manifest.get("edgeChunks")
        if not edge_chunks and manifest.get("edgeChunk"):
            edge_chunks = [manifest["edgeChunk"]]
        for c in (edge_chunks or []):
            refs.append(_ref(c.get("dataCid"), c.get("contentId")))
    elif kind == "view":
        vc = manifest.get("viewChunk") or {}
        refs.append(_ref(vc.get("dataCid"), vc.get("contentId")))
    elif kind == "linkset":
        for c in manifest.get("linkChunks", []):
            refs.append(_ref(c.get("dataCid"), c.get("contentId")))
    return [r for r in refs if r is not None]


def blob_cids(manifest: dict) -> list[str]:
    """The stable blob identities a manifest references — the diff/dedup keys."""
    return [r.content_id for r in blob_refs(manifest)]


class TreeDiff:
    """added — blob ids in the child but not the parent (new data to index).
    removed — blob ids in the parent but not the child (deleted data to drop)."""

    __slots__ = ("added", "removed")

    def __init__(self, added: list[str], removed: list[str]):
        self.added = added
        self.removed = removed

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"TreeDiff(added={self.added!r}, removed={self.removed!r})"


def diff_trees(parent: dict | None, child: dict) -> TreeDiff:
    """Structural diff between two tree snapshots. Because blobs are
    content-addressed, an unchanged page keeps its id and appears in neither list
    — so a commit that touches k of n pages diffs to O(k), not O(n). ``parent``
    None => root commit (everything is added)."""
    child_cids = _dedup(blob_cids(child))
    parent_cids = set(blob_cids(parent) if parent else [])
    child_set = set(child_cids)
    return TreeDiff(
        added=[c for c in child_cids if c not in parent_cids],
        removed=[c for c in _dedup(blob_cids(parent) if parent else []) if c not in child_set],
    )


def _dedup(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# DELTA PLAN — what an incremental build must fetch and tombstone
# ---------------------------------------------------------------------------
class DeltaPlan:
    """A build plan expressed in *retrieval uris*, ready to hand to the fetcher.

    added_uris   — blobs present in the child tree but not the parent. Fetch and
                   embed these (the only new data).
    removed_uris — blobs present in the parent tree but not the child. Fetch these
                   from the *parent* to learn which entities they held, then delete
                   those points from the index (delete propagation).

    Both are keyed on content identity (blob_cids) but returned as the child's /
    parent's uris so a caller can fetch them directly. A blob whose bytes are
    unchanged keeps its content id and lands in neither list — that's the whole
    point: a commit touching k of n pages plans O(k) work, not O(n).
    """

    __slots__ = ("added_uris", "removed_uris")

    def __init__(self, added_uris: list[str], removed_uris: list[str]):
        self.added_uris = added_uris
        self.removed_uris = removed_uris

    def is_empty(self) -> bool:
        return not self.added_uris and not self.removed_uris

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"DeltaPlan(added={self.added_uris!r}, removed={self.removed_uris!r})"


def collect_removed_point_ids(removed_uris, blobs_by_uri, point_id_fn) -> list:
    """Delete-propagation core: map removed blob uris → the index point ids to
    delete, via a caller-supplied ``point_id_fn(record) -> id``. Pure (no IPFS /
    Qdrant), so the fan-out + de-dup logic is unit-testable without heavy deps;
    the real id derivation lives with the embedder (``embeddings._blob_point_ids``)
    so the ids here match exactly what was upserted."""
    ids: list = []
    for uri in removed_uris:
        for r in (blobs_by_uri.get(uri) or []):
            if isinstance(r, dict):
                ids.append(point_id_fn(r))
    seen: set = set()
    out: list = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def plan_delta(child: dict, parent: dict | None) -> DeltaPlan:
    """Turn a parent→child tree diff into a fetch plan. ``parent`` None => root
    commit (every child blob is added, nothing removed)."""
    child_refs = blob_refs(child)
    parent_refs = blob_refs(parent) if parent else []
    child_ids = {r.content_id for r in child_refs}
    parent_ids = {r.content_id for r in parent_refs}

    seen_add: set[str] = set()
    added_uris: list[str] = []
    for r in child_refs:
        if r.content_id not in parent_ids and r.content_id not in seen_add:
            seen_add.add(r.content_id)
            added_uris.append(r.uri)

    seen_rem: set[str] = set()
    removed_uris: list[str] = []
    for r in parent_refs:
        if r.content_id not in child_ids and r.content_id not in seen_rem:
            seen_rem.add(r.content_id)
            removed_uris.append(r.uri)

    return DeltaPlan(added_uris, removed_uris)
