"""
mcp_server.py — Model Context Protocol layer over the Fangorn Semantic CDN.

A SELF-CONTAINED, LOCAL PULL-CLIENT that exposes on-chain-published knowledge to
agents (Claude, autonomous trading agents, mobile assistants) as well-typed tools.
It pulls a dataset's immutable shards from a Semantic CDN (cdn.py) into an
in-process index and searches them LOCALLY — the agent's query vector never leaves
this process. That is the "intent is private" half of the Fangorn thesis
("knowledge is public, intent is private"), applied to the agent path.

Why this is a rewrite (vs. the old server-proxy MCP)
----------------------------------------------------
The previous MCP was a thin proxy over `quickbeam serve` (server.py), which joins
records across schemas and flattens them through a heuristic `role_map` into
title/subtitle/tags. That projection is lossy — many datasets infer an *empty*
role_map (robinhood's is all-null), so the agent got null titles and no tags — and
it was built for a UI renderer, not an agent. An LLM reasons over raw JSON fine, so
this server hands back the RAW record fields plus first-class on-chain provenance
and lets the agent navigate TWO axes:

    • semantic   — vector similarity           (search)
    • relational — typed linkset edges          (neighbors)   ← the knowledge-mesh axis

The relational axis is what makes this a mesh an agent can *walk* — "what is
connected to NVDA, and how" — not just a search box. A record's id is its graph
node endpoint (e.g. ``rh:asset:NVDA``) and linkset edges (``{rel, from, to}``)
reference those same ids, so traversal is a join on the id namespace.

Tools
-----
    list_datasets()                    what knowledge exists (the CDN catalog)
    describe(dataset)                  entity types, field vocab, relations, embed contract
    search(dataset, query, ...)        semantic search → raw records + provenance
    get(dataset, id)                   one record, full fields + provenance
    neighbors(dataset, id, rel, ...)   walk the linkset edges (relational axis)
    aggregate(dataset, group_by, ...)  group-by/reduce server-side → the answer, not the data
    export(dataset, entity_type, ...)  projected slice → local file PATH (bulk offline analytics)

Three axes, not two: `search` is semantic and `neighbors` is relational, but both
hand back per-record payloads — unsuited to corpus-wide analytics, where streaming
every record through the agent's context is the dominant cost. `aggregate` reduces
server-side and returns an N-row table; `export` spills a projected column slice to
local disk and returns only its path, so the agent queries it with its own code
(duckdb/pandas) instead of paying to move the bytes through the model.

Run
---
    # remote streamable-http (agents connect here):
    quickbeam mcp --transport http --host 0.0.0.0 --port 8765 \
        --cdn-url http://localhost:8090

    # local stdio (MCP Inspector / Claude Desktop):
    quickbeam mcp --transport stdio --cdn-url http://localhost:8090

    # Phase 2 — charge external agents per search/neighbors call:
    quickbeam mcp --transport http --x402-pay-to 0xRECV --x402-price 0.001

Relational-axis delivery
------------------------
The CDN currently delivers the semantic axis (embedded record shards) but not yet
the linkset edges. Until it does, `neighbors` sources edges from a local linkset
via ``QUICKBEAM_EDGES`` (a JSON file, or a directory holding ``<dataset>.json``);
each file is a list of ``{rel, from, to, fromType, toType}`` edges — the exact
shape linkgen/robinhood stage. When the CDN grows a ``/domains/{name}/edges``
endpoint, this server picks it up automatically with no tool changes.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone

import httpx
import numpy as np
from fastmcp import FastMCP

# The query vector MUST go through the identical transform the builder applied to
# documents (LayerNorm → Matryoshka slice → L2-normalize) or cosine similarity is
# corrupted. Reuse the single source of truth rather than re-deriving it here.
from quickbeam.embeddings import matryoshka

# ---------------------------------------------------------------------------
# CONFIG — resolved from env at import; overridable in main().
# ---------------------------------------------------------------------------
CDN_URL     = os.environ.get("QUICKBEAM_CDN_URL", "http://localhost:8090")
HTTP_TIMEOUT = float(os.environ.get("QUICKBEAM_CDN_TIMEOUT", "60"))
# Optional local linkset source for the relational axis (file or directory).
EDGES_PATH  = os.environ.get("QUICKBEAM_EDGES")
# nomic-embed-text-v1.5 is asymmetric; queries must be prefixed "search_query:".
QUERY_PREFIX = "search_query"

# Phase 2 gate; stays None (and dormant) unless main() enables payments.
_GATE = None

mcp = FastMCP("quickbeam")


# ---------------------------------------------------------------------------
# HTTP PLUMBING (Semantic CDN client). Tests monkeypatch _get_json/_get_bytes.
# ---------------------------------------------------------------------------
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=CDN_URL, timeout=HTTP_TIMEOUT)


async def _get_json(path: str) -> dict:
    async with _client() as client:
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.json()


async def _get_bytes(path: str) -> bytes:
    async with _client() as client:
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.content


# ---------------------------------------------------------------------------
# QUERY EMBEDDING (in-process — the query never leaves this box)
# ---------------------------------------------------------------------------
# One fastembed engine per model name. Built lazily on first search so importing
# this module (and running the free catalog/describe tools) never loads a model.
_embedders: dict[str, object] = {}


def _embed_raw(model: str, texts: list[str]) -> list[list[float]]:
    """Full-width document embeddings for `texts` under `model`. Synchronous and
    CPU/GPU-bound — callers run it off the event loop. Tests monkeypatch this to
    avoid downloading a model."""
    eng = _embedders.get(model)
    if eng is None:
        from fastembed import TextEmbedding
        eng = TextEmbedding(model_name=model, max_length=256)
        _embedders[model] = eng
    return [v.tolist() if hasattr(v, "tolist") else list(v)
            for v in eng.embed(texts, batch_size=64)]


async def _embed_query(model: str, dim: int, text: str) -> np.ndarray:
    raw = (await asyncio.to_thread(_embed_raw, model, [f"{QUERY_PREFIX}: {text}"]))[0]
    # matryoshka: LayerNorm over the full vector → slice to dim → L2-normalize.
    return np.asarray(matryoshka(raw, dim), dtype=np.float32)


# ---------------------------------------------------------------------------
# IN-MEMORY DATASET (pulled shards; brute-force cosine)
# ---------------------------------------------------------------------------
# CDN snapshots are baked to be pullable (often small enough for in-browser
# clients), so an exact brute-force cosine over an in-memory matrix is both
# simplest and best here — no Qdrant server, no external process, nothing to leak.
class _Dataset:
    __slots__ = ("name", "manifest", "model", "dim", "distance",
                 "records", "_by_id", "vecs", "edges")

    def __init__(self, name: str, manifest: dict):
        self.name = name
        self.manifest = manifest
        self.model = manifest.get("model")
        self.dim = manifest.get("dim")
        self.distance = manifest.get("distance", "Cosine")
        self.records: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self.vecs: np.ndarray | None = None
        self.edges: list[dict] | None = None  # None = not yet loaded

    def add(self, row: dict) -> list[float] | None:
        """Ingest one shard row ({track_id, fields, embedding, owner, meta}).
        Returns its embedding (for the matrix) or None to skip."""
        tid = row.get("track_id")
        vec = row.get("embedding")
        if not tid or not vec:
            return None
        fields = row.get("fields") or {}
        rec = {
            "id":         tid,
            "entityType": fields.get("entityType"),
            "owner":      row.get("owner"),
            "fields":     fields,
            "meta":       row.get("meta") or {},
        }
        self.records.append(rec)
        self._by_id[tid] = rec
        return vec

    def finalize(self, vectors: list[list[float]]) -> None:
        """Stack + L2-normalize the vectors so cosine reduces to a dot product."""
        if vectors:
            m = np.asarray(vectors, dtype=np.float32)
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self.vecs = m / norms
        else:
            self.vecs = np.zeros((0, self.dim or 0), dtype=np.float32)


_REGISTRY: dict[str, _Dataset] = {}
_registry_lock = asyncio.Lock()


async def _ensure_loaded(name: str) -> _Dataset:
    """Pull `name`'s shards into memory once; cached thereafter."""
    ds = _REGISTRY.get(name)
    if ds is not None:
        return ds
    async with _registry_lock:
        ds = _REGISTRY.get(name)
        if ds is not None:
            return ds
        manifest = await _get_json(f"/domains/{name}/manifest")
        ds = _Dataset(name, manifest)
        # Delta shards re-deliver updated records under the same track_id, so
        # dedupe last-wins across shards (manifest order) before ingesting;
        # tombstoned ids (delete propagation) are dropped entirely.
        rows_by_id: dict[str, dict] = {}
        for shard in manifest.get("shards", []):
            for row in await _shard_rows(name, shard):
                tid = row.get("track_id")
                if tid:
                    rows_by_id[tid] = row
        for tid in manifest.get("tombstones") or []:
            rows_by_id.pop(tid, None)
        vectors: list[list[float]] = []
        for row in rows_by_id.values():
            vec = ds.add(row)
            if vec is not None:
                vectors.append(vec)
        ds.finalize(vectors)
        _REGISTRY[name] = ds
        return ds


async def _shard_rows(name: str, shard: dict) -> list[dict]:
    """Download one gzipped NDJSON shard, verify it against its manifest sha256,
    and parse it into rows."""
    data = await _get_bytes(f"/domains/{name}/shards/{shard['file']}")
    expected = shard.get("sha256")
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"sha256 mismatch for {shard['file']}")
    rows: list[dict] = []
    for line in gzip.decompress(data).decode("utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# RELATIONAL AXIS — linkset edges ({rel, from, to, fromType, toType})
# ---------------------------------------------------------------------------
async def _ensure_edges(ds: _Dataset) -> list[dict]:
    """Load `ds`'s edges once. Prefers the CDN (future /edges endpoint), falls
    back to a local linkset via QUICKBEAM_EDGES. Empty list = none available."""
    if ds.edges is not None:
        return ds.edges
    edges = await _load_cdn_edges(ds.name)
    if edges is None:
        edges = _load_local_edges(ds.name)
    ds.edges = edges or []
    return ds.edges


async def _load_cdn_edges(name: str) -> list[dict] | None:
    """Try the (forthcoming) CDN linkset endpoint. Returns None if unavailable so
    the local fallback can take over — a 404 today is expected, not an error."""
    try:
        data = await _get_json(f"/domains/{name}/edges")
    except Exception:  # noqa: BLE001 — endpoint may not exist yet
        return None
    return _coerce_edges(data)


def _load_local_edges(name: str) -> list[dict] | None:
    """Load edges from QUICKBEAM_EDGES: a JSON file, or a directory holding
    <name>.json. The file is a list of {rel, from, to, ...} edges."""
    if not EDGES_PATH:
        return None
    path = EDGES_PATH
    if os.path.isdir(EDGES_PATH):
        path = os.path.join(EDGES_PATH, f"{name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return _coerce_edges(json.load(f))


def _coerce_edges(data) -> list[dict]:
    """Accept either a bare list of edges or {edges:[...]} / {links:[...]}."""
    if isinstance(data, dict):
        data = data.get("edges") or data.get("links") or []
    return [e for e in data if isinstance(e, dict) and e.get("from") and e.get("to")]


# ---------------------------------------------------------------------------
# RESULT SHAPING
# ---------------------------------------------------------------------------
def _iso8601(ts) -> str | None:
    """Best-effort ISO8601 from a unix-seconds blockTimestamp."""
    if ts in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _provenance(rec: dict) -> dict:
    """Where a record came from on-chain — a first-class field on every result."""
    meta = rec.get("meta") or {}
    return {
        "source_cid": meta.get("manifestCid"),
        "published":  _iso8601(meta.get("blockTimestamp")),
        "version":    meta.get("version"),
        "publisher":  meta.get("owner") or rec.get("owner"),
    }


def _shape(rec: dict, score: float | None = None) -> dict:
    """Agent-facing record: the RAW fields (no lossy role_map projection) plus
    identity, entity type, and provenance."""
    out = {
        "id":         rec["id"],
        "entityType": rec.get("entityType"),
        "fields":     rec.get("fields") or {},
        "provenance": _provenance(rec),
    }
    if score is not None:
        out["score"] = round(float(score), 6)
    return out


# ---------------------------------------------------------------------------
# ANALYTICAL AXIS — structured filter / group-by / bulk export
# ---------------------------------------------------------------------------
# search() is semantic and returns fat records; it is the WRONG primitive for
# corpus-wide analytics (rank every wallet, sum flow per asset, dump a column
# for offline compute). Those jobs want the ANSWER, not the raw data streamed
# through the agent's context. These helpers run over the same in-memory records
# and either reduce server-side (aggregate) or spill to a local file (export).
def _num(x):
    """Best-effort float; None for non-numeric / missing (so sums skip them)."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return None
    return None


def _cmp(val, cond) -> bool:
    """One field predicate. `cond` is a scalar (equality) or a dict of operators
    {eq,ne,gt,gte,lt,lte,in,contains,exists}."""
    if not isinstance(cond, dict):
        return val == cond
    for op, operand in cond.items():
        if op == "eq" and val != operand:
            return False
        if op == "ne" and val == operand:
            return False
        if op in ("gt", "gte", "lt", "lte"):
            n, o = _num(val), _num(operand)
            if n is None or o is None:
                return False
            if op == "gt" and not n > o:
                return False
            if op == "gte" and not n >= o:
                return False
            if op == "lt" and not n < o:
                return False
            if op == "lte" and not n <= o:
                return False
        if op == "in" and val not in operand:
            return False
        if op == "contains" and (val is None or str(operand) not in str(val)):
            return False
        if op == "exists" and (val is not None) != bool(operand):
            return False
    return True


def _match_where(fields: dict, where: dict | None) -> bool:
    return not where or all(_cmp(fields.get(k), c) for k, c in where.items())


def _iter_records(ds: "_Dataset", entity_type: str | None, where: dict | None):
    for rec in ds.records:
        if entity_type is not None and rec.get("entityType") != entity_type:
            continue
        f = rec.get("fields") or {}
        if _match_where(f, where):
            yield rec


def _parse_measure(spec: str):
    """"count" | "sum:field" | "avg:field" | "min:field" | "max:field" |
    "distinct:field" → (fn, field)."""
    if spec in ("count", "count:*"):
        return "count", None
    fn, _, field = spec.partition(":")
    return fn, (field or None)


def _sort_key(v):
    """Type-safe sort key: numerics first (by value), then everything else by str."""
    n = _num(v)
    return (0, n) if n is not None else (1, "" if v is None else str(v))


def _do_aggregate(ds, group_by, measures, entity_type, where, order_by, limit):
    if isinstance(group_by, str):
        group_by = [group_by]
    measures = measures or {"count": "count"}
    groups: dict[tuple, dict] = {}
    for rec in _iter_records(ds, entity_type, where):
        f = rec.get("fields") or {}
        key = tuple(f.get(g) for g in group_by)
        acc = groups.get(key)
        if acc is None:
            acc = {m: {"sum": 0.0, "n": 0, "min": None, "max": None,
                       "set": set(), "cnt": 0} for m in measures}
            groups[key] = acc
        for name, spec in measures.items():
            fn, field = _parse_measure(spec)
            a = acc[name]
            if fn == "count":
                a["cnt"] += 1
            elif fn in ("distinct", "count_distinct"):
                a["set"].add(f.get(field))
            else:
                v = _num(f.get(field))
                if v is not None:
                    a["sum"] += v
                    a["n"] += 1
                    a["min"] = v if a["min"] is None else min(a["min"], v)
                    a["max"] = v if a["max"] is None else max(a["max"], v)
    rows = []
    for key, acc in groups.items():
        row = dict(zip(group_by, key))
        for name, spec in measures.items():
            fn, _f = _parse_measure(spec)
            a = acc[name]
            if fn == "count":
                row[name] = a["cnt"]
            elif fn in ("distinct", "count_distinct"):
                row[name] = len(a["set"])
            elif fn == "sum":
                row[name] = round(a["sum"], 6)
            elif fn == "avg":
                row[name] = round(a["sum"] / a["n"], 6) if a["n"] else None
            elif fn == "min":
                row[name] = a["min"]
            elif fn == "max":
                row[name] = a["max"]
        rows.append(row)
    total = len(rows)
    if order_by:
        desc = order_by.startswith("-")
        k = order_by[1:] if desc else order_by
        rows.sort(key=lambda r: _sort_key(r.get(k)), reverse=desc)
    if limit and limit > 0:
        rows = rows[:limit]
    return {"dataset": ds.name, "entity_type": entity_type, "group_by": group_by,
            "measures": measures, "groups": total, "rows": rows}


def _write_rows(rows: list[dict], fmt: str, stem: str):
    """Spill rows to a local file; return (path, bytes, count). ndjson or csv."""
    import tempfile
    import secrets
    export_dir = os.environ.get("QUICKBEAM_EXPORT_DIR") or \
        os.path.join(tempfile.gettempdir(), "quickbeam-exports")
    os.makedirs(export_dir, exist_ok=True)
    ext = "csv" if fmt == "csv" else "ndjson"
    path = os.path.join(export_dir, f"{stem}-{secrets.token_hex(3)}.{ext}")
    if fmt == "csv":
        import csv
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    else:
        with open(path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    return path, os.path.getsize(path), len(rows)


def _project_record(rec: dict, fields: list[str] | None) -> dict:
    f = rec.get("fields") or {}
    out = {"id": rec["id"], "entityType": rec.get("entityType")}
    if fields is None:
        out.update(f)
    else:
        for k in fields:
            out[k] = f.get(k)
    return out


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
@mcp.tool
async def list_datasets() -> dict:
    """List the knowledge datasets this server can navigate — the Semantic CDN
    catalog. Call this first to discover what is available.

    Each dataset is an on-chain-published corpus of records (e.g. tokenized
    assets, places, events). Returns, per dataset: `name` (pass it to the other
    tools), `description`, `count`, the `entity_types` it contains, embedding
    `dim`, and the source `collection`. Free — no payment required.
    """
    try:
        catalog = await _get_json("/catalog")
    except httpx.HTTPError as exc:
        return {"error": "catalog_unavailable", "detail": str(exc), "cdn": CDN_URL}
    datasets = [{
        "name":         d.get("name"),
        "description":  d.get("description", ""),
        "count":        d.get("count", 0),
        "entity_types": d.get("entity_types", []),
        "dim":          d.get("dim"),
    } for d in catalog.get("domains", []) if d.get("count", 0) > 0]
    return {
        "datasets":  datasets,
        "collection": catalog.get("collection"),
        "cdn":       CDN_URL,
    }


@mcp.tool
async def describe(dataset: str) -> dict:
    """Describe one dataset in depth so you know how to query it: its entity types
    (with counts), the field vocabulary its records actually carry, the
    relationship types available for `neighbors` traversal, and the embedding
    contract (model + dimensions). Call this before `search`/`neighbors` if you
    are unsure what a dataset holds. Free — no payment required.

    Args:
        dataset: The dataset name from `list_datasets`.
    """
    try:
        ds = await _ensure_loaded(dataset)
    except httpx.HTTPError as exc:
        return {"error": "dataset_unavailable", "dataset": dataset, "detail": str(exc)}

    field_keys: set[str] = set()
    for rec in ds.records:
        field_keys.update((rec.get("fields") or {}).keys())

    edges = await _ensure_edges(ds)
    relations = sorted({e.get("rel") for e in edges if e.get("rel")})

    return {
        "dataset":      dataset,
        "description":  ds.manifest.get("description", ""),
        "count":        len(ds.records),
        "entity_types": ds.manifest.get("entity_types", []),
        "fields":       sorted(field_keys),
        "relations":    relations,
        "relational_axis": "available" if edges else "not delivered for this dataset",
        "embed": {
            "model":    ds.model,
            "dim":      ds.dim,
            "distance": ds.distance,
        },
    }


@mcp.tool
async def search(dataset: str, query: str, limit: int = 10,
                 entity_type: str | None = None, owner: str | None = None,
                 payment: str | None = None) -> dict:
    """Search a dataset by MEANING, not keywords, and get back RAW records.

    The query is embedded locally (it never leaves this server) and matched against
    the dataset's vector space, so descriptive, vibe-based, and metaphorical
    phrasing all work — you do not need exact keywords. Use this whenever the user
    describes what they want in natural language ("chipmakers exposed to AI demand",
    "high-volatility meme stocks", "rainy-day melancholy piano").

    Args:
        dataset: The dataset name from `list_datasets`.
        query: A natural-language description of what to find.
        limit: Maximum number of results (default 10).
        entity_type: Optional — restrict to one entityType (see `describe`).
        owner: Optional — restrict to records published by this owner address.

    Returns:
        { "dataset", "results": [ {
              "id", "entityType", "fields": {..raw fields..}, "score",
              "provenance": { "source_cid", "published", "version", "publisher" }
          } ... ] }

    Results carry the RAW record fields (not a title/subtitle/tags projection) and
    on-chain `provenance` — cite it when the user cares about freshness or origin.
    Ordered by relevance `score` (higher is closer).
    """
    settlement = None
    if _GATE is not None:
        charge = _GATE.charge("search", payment)
        if not charge.ok:
            return charge.challenge      # x402 requirements back to the agent
        settlement = charge.settlement

    try:
        ds = await _ensure_loaded(dataset)
    except httpx.HTTPError as exc:
        return {"error": "dataset_unavailable", "dataset": dataset, "detail": str(exc)}
    if not ds.model or not ds.dim:
        return {"error": "no_embed_contract", "dataset": dataset}

    # Candidate mask (structured pre-filter), then cosine over the survivors.
    idx = np.arange(len(ds.records))
    if entity_type is not None:
        idx = idx[[ds.records[i].get("entityType") == entity_type for i in idx]]
    if owner is not None:
        idx = idx[[ds.records[i].get("owner") == owner for i in idx]]

    results: list[dict] = []
    if len(idx) and ds.vecs is not None and len(ds.vecs):
        q = await _embed_query(ds.model, ds.dim, query)
        scores = ds.vecs[idx] @ q
        order = np.argsort(-scores)[:max(0, limit)]
        results = [_shape(ds.records[idx[j]], float(scores[j])) for j in order]

    out = {"dataset": dataset, "results": results}
    if settlement is not None:
        out["payment"] = settlement
    return out


@mcp.tool
async def refresh(dataset: str) -> dict:
    """Re-pull a dataset from the CDN, picking up any delta shards / updates /
    tombstones published since it was first loaded. Datasets are cached in memory
    on first use, so a LIVE corpus (e.g. a market feed the pipeline re-publishes
    every few minutes) goes stale without this. Cheap: unchanged shards are
    immutable and HTTP-cached; only the new deltas actually download. Call it
    before time-sensitive analysis. Free — no payment required.

    Args:
        dataset: The dataset name from `list_datasets`.
    """
    async with _registry_lock:
        had = _REGISTRY.pop(dataset, None)
    try:
        ds = await _ensure_loaded(dataset)
    except httpx.HTTPError as exc:
        return {"error": "dataset_unavailable", "dataset": dataset, "detail": str(exc)}
    return {"dataset": dataset, "reloaded": True, "was_cached": had is not None,
            "count": len(ds.records),
            "created_at": ds.manifest.get("created_at"),
            "entity_types": ds.manifest.get("entity_types", [])}


@mcp.tool
async def get(dataset: str, id: str) -> dict:
    """Fetch a single record by its exact id, with all fields and provenance. The
    id is also the record's graph node endpoint (e.g. "rh:asset:NVDA") — pass it to
    `neighbors` to walk its relationships. Free — no payment required.

    Args:
        dataset: The dataset name from `list_datasets`.
        id: The exact record id (from a `search`/`neighbors` result).
    """
    try:
        ds = await _ensure_loaded(dataset)
    except httpx.HTTPError as exc:
        return {"error": "dataset_unavailable", "dataset": dataset, "detail": str(exc)}
    rec = ds._by_id.get(id)
    if rec is None:
        return {"error": "not_found", "dataset": dataset, "id": id}
    return {"dataset": dataset, "record": _shape(rec)}


@mcp.tool
async def neighbors(dataset: str, id: str, rel: str | None = None,
                    direction: str = "both", limit: int = 25,
                    payment: str | None = None) -> dict:
    """Walk the KNOWLEDGE GRAPH: return the records connected to `id` by typed
    linkset edges. This is the relational axis — "what is connected to NVDA, and
    how" — complementing `search`'s semantic axis.

    Edges are typed triples ({rel, from, to}); for example an Asset links to its
    Transfers ("hasTransfer"), corporate actions ("hasAction"), and oracle updates
    ("hasOracleUpdate"). A neighbor that is embedded in this dataset comes back with
    its full `fields`; one that lives outside it comes back as an endpoint (id +
    type) you can still reason over.

    Args:
        dataset: The dataset name from `list_datasets`.
        id: The record/node id to expand (from `search`/`get`).
        rel: Optional — only follow this relation (see `describe`.relations).
        direction: "out" (id → others), "in" (others → id), or "both" (default).
        limit: Maximum number of neighbors (default 25).

    Returns:
        { "dataset", "id", "neighbors": [ {
              "rel", "direction", "id", "entityType", "fields"?, "provenance"?
          } ... ] }
    """
    settlement = None
    if _GATE is not None:
        charge = _GATE.charge("neighbors", payment)
        if not charge.ok:
            return charge.challenge
        settlement = charge.settlement

    try:
        ds = await _ensure_loaded(dataset)
    except httpx.HTTPError as exc:
        return {"error": "dataset_unavailable", "dataset": dataset, "detail": str(exc)}

    edges = await _ensure_edges(ds)
    if not edges:
        return {"dataset": dataset, "id": id, "neighbors": [],
                "note": "relational layer not delivered for this dataset "
                        "(no linkset via CDN or QUICKBEAM_EDGES)"}

    want_out = direction in ("out", "both")
    want_in = direction in ("in", "both")
    hits: list[dict] = []
    for e in edges:
        if rel is not None and e.get("rel") != rel:
            continue
        if want_out and e.get("from") == id:
            nid, ntype, d = e.get("to"), e.get("toType"), "out"
        elif want_in and e.get("to") == id:
            nid, ntype, d = e.get("from"), e.get("fromType"), "in"
        else:
            continue
        entry = {"rel": e.get("rel"), "direction": d, "id": nid, "entityType": ntype}
        neighbor = ds._by_id.get(nid)
        if neighbor is not None:
            entry["fields"] = neighbor.get("fields") or {}
            entry["provenance"] = _provenance(neighbor)
        hits.append(entry)
        if len(hits) >= max(0, limit):
            break

    out = {"dataset": dataset, "id": id, "neighbors": hits}
    if settlement is not None:
        out["payment"] = settlement
    return out


@mcp.tool
async def aggregate(dataset: str, group_by, measures: dict | None = None,
                    entity_type: str | None = None, where: dict | None = None,
                    order_by: str | None = None, limit: int = 50) -> dict:
    """Group records SERVER-SIDE and return only the aggregated rows — the answer,
    not the raw data. Use this instead of pulling a whole entity type through
    `search` when the question is "how much / how many / top-N per X": largest
    traders, flow per asset, holders by sector, transfer counts per wallet.

    The heavy reduction happens here over the in-memory records, so a 3,500-row
    corpus comes back as an N-row table — no large result set crosses the context.

    Args:
        dataset: The dataset name from `list_datasets`.
        group_by: A field name (or list of field names) to group on, e.g.
            "fromAddr" or ["symbol", "signal"].
        measures: Map of output_name → aggregation, where each aggregation is one
            of "count", "sum:FIELD", "avg:FIELD", "min:FIELD", "max:FIELD",
            "distinct:FIELD". Defaults to {"count": "count"}.
        entity_type: Optional — restrict to one entityType before grouping
            (e.g. "Transfer"), see `describe`.
        where: Optional field predicates ANDed together. Each value is a scalar
            (equality) or a dict of operators: {gt,gte,lt,lte,ne,in,contains,exists}.
            Example: {"symbol": "BABA", "value": {"gte": 1}}.
        order_by: Output column to sort by; prefix "-" for descending
            (e.g. "-usd"). Numerics sort numerically.
        limit: Max rows returned after ordering (default 50; 0 = all).

    Returns:
        { "dataset", "entity_type", "group_by", "measures", "groups": <total
          distinct groups>, "rows": [ {<group fields>, <measures>} ... ] }

    Example — largest traders by outbound activity and token volume:
        aggregate("robinhood", "fromAddr", entity_type="Transfer",
                  measures={"legs": "count", "tok": "sum:value"}, order_by="-legs")
    Free — no payment required.
    """
    try:
        ds = await _ensure_loaded(dataset)
    except httpx.HTTPError as exc:
        return {"error": "dataset_unavailable", "dataset": dataset, "detail": str(exc)}
    try:
        return _do_aggregate(ds, group_by, measures, entity_type, where, order_by, limit)
    except Exception as exc:  # noqa: BLE001 — surface bad measure/field specs to the agent
        return {"error": "aggregate_failed", "dataset": dataset, "detail": str(exc)}


@mcp.tool
async def export(dataset: str, entity_type: str | None = None,
                 fields: list[str] | None = None, kind: str = "records",
                 where: dict | None = None, format: str = "ndjson") -> dict:
    """Spill a PROJECTED slice of the dataset to a LOCAL FILE and return its PATH —
    not the rows. Use for corpus-wide analytics an agent runs in its own code:
    pull only the columns you need, then query the file with duckdb/pandas/jq.
    This is how you get the full 3,500-record transfer log without streaming
    megabytes of JSON through the model context.

    Co-located only: the file is written to this server's local disk (the pull-
    client is designed to sit on the same box as its consumer). The returned path
    is absolute.

    Args:
        dataset: The dataset name from `list_datasets`.
        entity_type: Optional — restrict to one entityType (e.g. "Transfer").
        fields: Optional list of field names to project. Omit for all fields.
            Each output row is FLAT — {id, entityType, <fields...>} — so it loads
            straight into a dataframe or SQL table.
        kind: "records" (default) or "edges" (dump the linkset {rel,from,to,
            fromType,toType} for graph/cycle analysis — `entity_type`/`fields`
            are ignored).
        where: Optional field predicates (same grammar as `aggregate.where`),
            applied to records only.
        format: "ndjson" (default) or "csv".

    Returns:
        { "dataset", "kind", "path", "rows", "bytes", "format", "fields" }

    Example — the four columns needed for wallet-flow analysis:
        export("robinhood", entity_type="Transfer",
               fields=["symbol", "fromAddr", "toAddr", "value"])
    Free — no payment required.
    """
    try:
        ds = await _ensure_loaded(dataset)
    except httpx.HTTPError as exc:
        return {"error": "dataset_unavailable", "dataset": dataset, "detail": str(exc)}

    fmt = "csv" if format == "csv" else "ndjson"
    if kind == "edges":
        edges = await _ensure_edges(ds)
        rows = [{"rel": e.get("rel"), "from": e.get("from"), "to": e.get("to"),
                 "fromType": e.get("fromType"), "toType": e.get("toType")}
                for e in edges]
        stem = f"{dataset}-edges"
        out_fields = ["rel", "from", "to", "fromType", "toType"]
    else:
        rows = [_project_record(rec, fields)
                for rec in _iter_records(ds, entity_type, where)]
        stem = f"{dataset}-{entity_type or 'all'}"
        out_fields = fields
    try:
        path, nbytes, count = _write_rows(rows, fmt, stem)
    except OSError as exc:
        return {"error": "export_failed", "dataset": dataset, "detail": str(exc)}
    return {"dataset": dataset, "kind": kind, "path": path, "rows": count,
            "bytes": nbytes, "format": fmt, "fields": out_fields}


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
def main() -> None:
    global CDN_URL, EDGES_PATH, _GATE

    parser = argparse.ArgumentParser(description="quickbeam MCP server (Semantic CDN pull-client)")
    parser.add_argument("--cdn-url", default=CDN_URL,
                        help="Base URL of the Semantic CDN (cdn serve).")
    parser.add_argument("--edges", default=EDGES_PATH, metavar="PATH",
                        help="Local linkset JSON file or directory (relational axis) "
                             "until the CDN delivers edges.")
    parser.add_argument("--transport", default="http",
                        choices=["http", "streamable-http", "stdio", "sse"],
                        help="MCP transport (default: http = streamable-http).")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (http/sse).")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (http/sse).")
    # ── Phase 2: charge external agents per gated tool call ──────────────────
    x = parser.add_argument_group("x402 per-tool payment (Phase 2)")
    x.add_argument("--x402-pay-to", default=None, metavar="0x...",
                   help="Recipient address. Enables per-tool payment when set.")
    x.add_argument("--x402-price", default="0.001", metavar="USDC",
                   help="Price per gated tool call in whole token units.")
    x.add_argument("--x402-network", default="base-sepolia")
    x.add_argument("--x402-asset", default=None, metavar="0x...")
    x.add_argument("--x402-decimals", type=int, default=6)
    x.add_argument("--x402-facilitator", default=None, metavar="URL")
    args = parser.parse_args()

    CDN_URL = args.cdn_url.rstrip("/")
    EDGES_PATH = args.edges

    if args.x402_pay_to:
        try:
            from quickbeam.mcp_payments import build_gate
        except ImportError:
            from mcp_payments import build_gate
        _GATE = build_gate(
            pay_to=args.x402_pay_to, price=args.x402_price,
            network=args.x402_network, asset=args.x402_asset,
            decimals=args.x402_decimals, facilitator_url=args.x402_facilitator,
        )
        print(f"[mcp] Phase 2 payments ENABLED — {args.x402_price} per gated call "
              f"to {args.x402_pay_to}")
    else:
        print("[mcp] Phase 1 — tools are free (no --x402-pay-to)")

    print(f"[mcp] quickbeam MCP → CDN {CDN_URL} | edges={EDGES_PATH or '(none)'} "
          f"| transport={args.transport}")
    transport = "http" if args.transport == "streamable-http" else args.transport
    if transport in ("http", "sse"):
        mcp.run(transport=transport, host=args.host, port=args.port)
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
