# bundle_join.py
from typing import Callable, Awaitable, Any
import asyncio

# A fetch fn you already have: CID -> parsed JSON. Reuse the builder's IPFS resolver.
FetchJson = Callable[[str], Awaitable[Any]]


async def resolve_bundle(manifest_cid: str, fetch: FetchJson) -> dict:
    """Fetch a v3 bundle manifest and all its chunks. Returns the in-memory graph."""
    manifest = await fetch(manifest_cid)
    if manifest.get("version") != 3:
        raise ValueError(f"{manifest_cid} is not a v3 bundle manifest (got {manifest.get('version')})")

    # fetch node chunks (each is a list[BundleNode]) and the edge chunk concurrently
    node_chunk_cids = [c["dataCid"] for c in manifest["nodeChunks"]]
    edge_cid = manifest["edgeChunk"]["dataCid"]

    results = await asyncio.gather(
        *[fetch(cid) for cid in node_chunk_cids],
        fetch(edge_cid),
        return_exceptions=True,
    )
    *node_arrays, edges = results

    # surface a bad chunk loudly rather than silently dropping nodes
    for cid, arr in zip(node_chunk_cids, node_arrays):
        if isinstance(arr, Exception):
            raise RuntimeError(f"failed to fetch node chunk {cid}: {arr}")
    if isinstance(edges, Exception):
        raise RuntimeError(f"failed to fetch edge chunk {edge_cid}: {edges}")

    # index every node by id (ids are unique across the whole bundle — publisher enforces it)
    nodes_by_id: dict[str, dict] = {}
    for arr in node_arrays:
        for node in arr:
            nodes_by_id[node["id"]] = node

    return {"nodes_by_id": nodes_by_id, "edges": edges or []}


def join_bundle(graph: dict, root_type: str) -> list[dict]:
    """
    Walk edges from each root node and produce one merged record per root.

    Merged record shape:
      {
        "id": <root node id>,
        "fields": { ...root fields... },
        "<rel>": [ { ...neighbor fields... }, ... ]   # one key per outgoing relation
      }

    NOTE: this is the *structured* join. It does NOT build embedding strings —
    that's the embedder's job. Neighbors are returned as lists (1:many safe).
    """
    nodes_by_id = graph["nodes_by_id"]
    edges = graph["edges"]

    # group outgoing edges by (from_id, rel) once, instead of scanning per root
    out: dict[tuple[str, str], list[str]] = {}
    for e in edges:
        out.setdefault((e["from"], e["rel"]), []).append(e["to"])

    records = []
    for node in nodes_by_id.values():
        if node.get("type") != root_type:
            continue

        merged: dict[str, Any] = {"id": node["id"], "fields": dict(node["fields"])}

        # attach every outgoing relation as a list of neighbor field-dicts
        for (from_id, rel), to_ids in out.items():
            if from_id != node["id"]:
                continue
            merged[rel] = [
                nodes_by_id[tid]["fields"]
                for tid in to_ids
                if tid in nodes_by_id
            ]

        records.append(merged)

    return records


async def records_from_manifest(manifest_cid: str, root_type: str, fetch: FetchJson) -> list[dict]:
    """Convenience: CID -> merged records. What the builder calls per event."""
    graph = await resolve_bundle(manifest_cid, fetch)
    return join_bundle(graph, root_type)