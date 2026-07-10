"""Record-set → graph-node adapter.

Fangorn's git-native commit flow (`fangorn repo init` / `commit` / `push`) produces
a `"record-set"` manifest: a flat list of `entries`, each just a pointer
(`fields.dataCid` / `fields.contentId`) at one record's own blob — a different
on-chain shape from the typed node/edge `"bundle"` manifest that `bundle.py`
otherwise consumes (`fangorn.publisher.publishBundle`).

Record-set data has no declared node *types* the way a bundle does, so every
resolved record is tagged uniformly with the driving profile's `root_type` — the
whole point of a record-set schema is that it's homogeneous (one Fangorn schema =
one shape). Parent/child structure, if the schema carries one (e.g. the markdown
`filename`/`parentId` shape from the quickstart guide), is recovered by matching
each record's `parentId` field against a sibling's own local id.
"""
from quickbeam.ingest.sources.ipfs import fetch_all_ipfs


def _record_local_id(fields: dict, fallback_name=None) -> str | None:
    """The join key a record exposes to its siblings — whichever of these fields
    it carries first. `filename` covers the quickstart guide's markdown schema
    (where `parentId` points at a sibling's `filename`); `id`/`name` cover any
    other record-set schema that declares its own identity field."""
    for k in ("id", "filename", "name"):
        v = fields.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return fallback_name or None


def _entries_to_nodes(entries: list[dict], record_type: str) -> list[dict]:
    """Resolved record-set entries -> node dicts shaped like a bundle's own nodes
    (`{id, type, fields}`), so the shared `_project`/`_project_records` walk in
    `projection.py` can't tell the difference."""
    nodes = []
    for entry in entries:
        fields = dict(entry.get("fields") or {})
        local_id = _record_local_id(fields, entry.get("name"))
        if local_id is None:
            continue
        nodes.append({"id": local_id, "type": record_type, "fields": fields})
    return nodes


def _entries_to_edge_pairs(nodes: list[dict]):
    """Yield (child_id, parent_id) pairs for every node whose `parentId` field
    resolves to a sibling in this same manifest. Schemas without a `parentId`
    field simply yield no edges — each record still projects fine on its own
    fields, just with nothing folded in from neighbors."""
    ids = {n["id"] for n in nodes}
    for n in nodes:
        parent = n["fields"].get("parentId")
        if isinstance(parent, str) and parent.strip() and parent in ids:
            yield (n["id"], parent)


async def resolve_record_entries(entries, ipfs_gateway, ipfs_timeout, concurrency,
                                  headers, desc="Records"):
    """Resolve a record-set manifest's entries (dataCid pointers) into their actual
    field payloads. Mirrors `server.py`'s `fetch_schema_entries`, which already does
    this for the raw-join CDN path — a blob can hold a single record ({"fields":
    {...}} or a bare fields dict) or a list of them."""
    data_cids = list({
        e["fields"]["dataCid"] for e in entries
        if isinstance((e.get("fields") or {}).get("dataCid"), str)
    })
    if not data_cids:
        return []

    payloads = await fetch_all_ipfs(data_cids, ipfs_gateway, ipfs_timeout, concurrency,
                                     desc=desc, headers=headers)

    resolved = []
    for e in entries:
        fields = e.get("fields") or {}
        dcid = fields.get("dataCid")
        payload = payloads.get(dcid) if dcid else None
        if payload is None:
            continue
        raw_records: list = []
        if isinstance(payload, list):
            raw_records = [r for r in payload if isinstance(r, dict)]
        elif isinstance(payload, dict):
            if isinstance(payload.get("fields"), dict):
                raw_records = [payload]
            else:
                raw_records = [{"fields": payload, "name": payload.get("name", "")}]
        for r in raw_records:
            rf = r["fields"] if isinstance(r.get("fields"), dict) else r
            resolved.append({"name": r.get("name", ""), "fields": rf})
    return resolved
