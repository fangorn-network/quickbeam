import certifi
import os

# ── Native thread governance ────────────────────────────────────────────────
# numba (pulled in by UMAP), numpy/OpenBLAS, and onnxruntime (via fastembed)
# each start a pool of worker threads that *busy-wait* when idle. After a big
# job like the 860k-point UMAP build finishes, those leftover threads keep every
# core pinned at 100% for the life of the process. Make idle OpenMP threads
# sleep, and cap each pool to ~half the cores so a build can't lock up the whole
# machine. Must run before those libraries load (fastembed below pulls in
# onnxruntime at import time; numpy/numba load lazily on the first map build).
_HALF_CORES = str(max(1, (os.cpu_count() or 4) // 2))
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("OMP_NUM_THREADS", _HALF_CORES)
os.environ.setdefault("OPENBLAS_NUM_THREADS", _HALF_CORES)
os.environ.setdefault("MKL_NUM_THREADS", _HALF_CORES)
os.environ.setdefault("NUMEXPR_NUM_THREADS", _HALF_CORES)
os.environ.setdefault("NUMBA_NUM_THREADS", _HALF_CORES)

import io
import sys
os.environ['SSL_CERT_FILE'] = certifi.where()
import argparse
import asyncio
import threading
import aiohttp
import random
import hashlib
import json
import logging
import math
import uuid
import requests
import uvicorn

from qdrant_client import QdrantClient, models as qmodels
from fastembed import TextEmbedding
from fastapi import FastAPI, Query, BackgroundTasks, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
# Dual import: works both as a script run from quickbeam/ and as quickbeam.server.
try:
    from quickbeam.roles import infer_roles, field_label
except ImportError:
    from roles import infer_roles, field_label
try:
    from quickbeam import x402 as x402mod
except ImportError:
    import x402 as x402mod

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ---------------------------------------------------------------------------
# ARGS
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Fangorn Qdrant server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--schema", "-s", metavar="NAME=0x...", action="append", dest="schemas", default=[])
    parser.add_argument("--primary", "-p", metavar="NAME", default=None)
    parser.add_argument(
        "--subgraph-url",
        default="https://gateway.thegraph.com/api/subgraphs/id/8SgbhtiitpAhEfyTgeAHxHH5DQ2gTygUuXgc3b7MCFyc",
    )
    parser.add_argument("--graph-api-key",   default="")
    parser.add_argument("--ipfs-gateway",    default="https://gateway.pinata.cloud/ipfs")
    parser.add_argument("--qdrant-url",      default=None,  metavar="URL", help="Qdrant Cloud URL e.g. https://xyz.cloud.qdrant.io:6334 (overrides --qdrant-host/port)")
    parser.add_argument("--qdrant-api-key",  default=None,  help="Qdrant Cloud API key")
    parser.add_argument("--qdrant-host",     default="localhost")
    parser.add_argument("--qdrant-port",     type=int, default=6333)
    parser.add_argument("--qdrant-grpc-port", type=int, default=6334)
    parser.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    parser.add_argument("--collection",      default="fangorn")
    parser.add_argument(
        "--dim", type=int, default=None,
        help="Vector dimension. Default: read from the recovered collection and "
             "Matryoshka-truncate query embeddings to match it.",
    )
    parser.add_argument("--searchable-fields", default="auto")
    parser.add_argument("--page-size",       type=int, default=100)
    parser.add_argument("--ipfs-timeout",    type=int, default=20)
    parser.add_argument("--concurrency",     type=int, default=16)
    parser.add_argument("--host",            default="0.0.0.0")
    parser.add_argument("--port",            type=int, default=8080)
    parser.add_argument("--reset",           action="store_true", default=False)
    parser.add_argument("--embedding-model", default="nomic-ai/nomic-embed-text-v1.5")
    parser.add_argument(
        "--bundle-cid", default=None, metavar="CID",
        help="IPFS CID of an NDJSON bundle to seed from on first startup (skipped if collection already has points)",
    )
    parser.add_argument(
        "--catalog-map-file", default="./db/catalog_map.json.gz", metavar="PATH",
        help="Gzipped catalog-map artifact produced by `quickbeam build --umap-target file`. "
             "When present, GET /catalog/map streams it directly (no UMAP recompute) — the "
             "scalable path for large collections.",
    )
    # ── x402 payment gating ──────────────────────────────────────────────────
    # When --x402-pay-to is set, the search/query routes return HTTP 402 until a
    # valid X-PAYMENT header is supplied. See quickbeam/x402.py.
    x = parser.add_argument_group("x402 payment gating")
    x.add_argument("--x402-pay-to", default=None, metavar="0x...",
                   help="Recipient address for payments. Enables x402 gating when set.")
    x.add_argument("--x402-price", default="0.001", metavar="USDC",
                   help="Price per gated request in whole token units (default: 0.001).")
    x.add_argument("--x402-network", default="base-sepolia",
                   help="EVM network: base-sepolia, base, avalanche-fuji.")
    x.add_argument("--x402-asset", default=None, metavar="0x...",
                   help="Token contract. Defaults to the network's USDC.")
    x.add_argument("--x402-decimals", type=int, default=6,
                   help="Token decimals for converting --x402-price to atomic units.")
    x.add_argument("--x402-facilitator", default=None, metavar="URL",
                   help="Facilitator URL for on-chain verify+settle. Omit for local verification.")
    return parser.parse_args()

MODEL_DIM_MAP = {
    "nomic-ai/nomic-embed-text-v1.5":           768,
    "BAAI/bge-small-en-v1.5":                   384,
    "sentence-transformers/all-MiniLM-L6-v2":   384,
}

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

cfg: argparse.Namespace = None

def get_schemas() -> dict[str, str]:
    schemas = {}
    for pair in cfg.schemas:
        if "=" not in pair:
            raise ValueError(f"--schema must be NAME=0x..., got: {pair!r}")
        name, schema_id = pair.split("=", 1)
        schemas[name.strip()] = schema_id.strip()
    if not schemas:
        raise ValueError("At least one --schema NAME=0x... is required")
    return schemas

def get_primary(schemas: dict[str, str]) -> str:
    if cfg.primary:
        if cfg.primary not in schemas:
            raise ValueError(f"--primary '{cfg.primary}' not found in --schema keys: {list(schemas.keys())}")
        return cfg.primary
    return next(iter(schemas))

def get_searchable_fields() -> set[str]:
    return set(f.strip() for f in cfg.searchable_fields.split(",") if f.strip())

qdrant_client:   QdrantClient | None = None
embed_engine:    TextEmbedding | None = None
debug_secondary: dict                 = {}
role_map_global: dict | None          = None
# Effective vector dimension, resolved at startup from the recovered collection
# (or --dim / model default as a fallback). Query embeddings are truncated to it.
vector_dim:      int | None           = None

# ---------------------------------------------------------------------------
# ID HELPERS
# Qdrant requires UUID or uint64 point IDs. We derive a deterministic UUID
# from the string track ID so the mapping is stable across restarts.
# ---------------------------------------------------------------------------

def _str_to_uuid(s: str) -> str:
    """Deterministic UUID v5 from an arbitrary string."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, s))

def _track_id_str(entry: dict) -> str:
    """Extract the canonical string track ID from an entry dict (same logic as ingest)."""
    fields = entry.get("fields", {}) or {}
    for key in ["trackId", "track_id", "id", "contentId", "dataCid"]:
        val = fields.get(key)
        if val and isinstance(val, str):
            val_clean = val.strip().removeprefix("track:")
            if val_clean and not val_clean.startswith("chunk:"):
                return val_clean
    for nest_key in ["content", "data", "metadata", "properties", "track"]:
        nest = fields.get(nest_key)
        if isinstance(nest, dict):
            for key in ["trackId", "track_id", "id", "contentId"]:
                val = nest.get(key)
                if val and isinstance(val, str):
                    val_clean = val.strip().removeprefix("track:")
                    if val_clean and not val_clean.startswith("chunk:"):
                        return val_clean
    artist = str(fields.get("byArtist") or fields.get("artist") or "").strip()
    title  = str(fields.get("title") or fields.get("name") or "").strip()
    if artist and title and not title.startswith("chunk:"):
        return hashlib.sha256(f"{artist}:{title}".encode()).hexdigest()[:24]
    name = entry.get("name", "").strip().removeprefix("track:")
    if not name or name.startswith("chunk:"):
        leaf = fields.get("leaf")
        if leaf and isinstance(leaf, str):
            return leaf.strip()
    return name

# ---------------------------------------------------------------------------
# ROLE ACCESSORS
# ---------------------------------------------------------------------------

def _rm() -> dict:
    return role_map_global or {}

def _role_value(payload: dict, role: str):
    field = _rm().get(role)
    return payload.get(field) if field else None

def _role_title(payload: dict, fallback: str = "") -> str:
    v = _role_value(payload, "title")
    return str(v) if v else fallback

def _role_subtitle(payload: dict) -> str:
    v = _role_value(payload, "subtitle")
    return str(v) if v else ""

def _role_tags(payload: dict) -> list[str]:
    out: list[str] = []
    for field in _rm().get("tags", []) or []:
        v = payload.get(field)
        if isinstance(v, list):
            out.extend(str(x) for x in v if x)
        elif v:
            out.append(str(v))
    return out

def _role_tag_field_values(payload: dict, idx: int) -> list[str]:
    fields = _rm().get("tags", []) or []
    if idx < len(fields):
        v = payload.get(fields[idx])
        if isinstance(v, list):
            return [str(x) for x in v if x]
        if v:
            return [str(v)]
    return []

def _role_spatial_xy(payload: dict):
    field = _rm().get("spatial")
    if not field:
        return None
    v = payload.get(field)
    if isinstance(v, dict):
        low = {str(k).lower(): val for k, val in v.items()}
        def num(*keys):
            for k in keys:
                if k in low and isinstance(low[k], (int, float)):
                    return float(low[k])
            return None
        lon = num("lon", "lng", "longitude", "x")
        lat = num("lat", "latitude", "y")
        if lon is None or lat is None:
            mnlon, mxlon = num("min_lon"), num("max_lon")
            mnlat, mxlat = num("min_lat"), num("max_lat")
            if None not in (mnlon, mxlon, mnlat, mxlat):
                lon, lat = (mnlon + mxlon) / 2, (mnlat + mxlat) / 2
        if lon is not None and lat is not None:
            return (lon, lat)
    return None

def _ensure_role_map() -> dict:
    global role_map_global
    if role_map_global is not None:
        return role_map_global
    sample: list[dict] = []
    try:
        records, _ = qdrant_client.scroll(
            collection_name=cfg.collection,
            limit=300,
            with_payload=True,
            with_vectors=False,
        )
        for pt in records:
            p = (pt.payload or {}).get("fields", {})
            if isinstance(p, dict):
                sample.append(p)
    except Exception:
        pass
    role_map_global = infer_roles(sample)
    return role_map_global

# ---------------------------------------------------------------------------
# COLLECTION COUNT HELPER
# ---------------------------------------------------------------------------

def _collection_count() -> int:
    try:
        info = qdrant_client.get_collection(cfg.collection)
        return info.points_count or 0
    except Exception:
        return 0

# ---------------------------------------------------------------------------
# SCROLL ALL — yields payloads + vectors in batches
# ---------------------------------------------------------------------------

def _scroll_all(with_vectors: bool = False, batch: int = 5_000):
    """Generator: yields (point_id, payload_dict, vector_or_None) for every point."""
    offset = None
    while True:
        records, next_offset = qdrant_client.scroll(
            collection_name=cfg.collection,
            limit=batch,
            offset=offset,
            with_payload=True,
            with_vectors=with_vectors,
        )
        for pt in records:
            vec = None
            if with_vectors:
                vec = pt.vector if isinstance(pt.vector, list) else (pt.vector.tolist() if pt.vector is not None else None)
            yield pt.id, pt.payload or {}, vec
        if next_offset is None:
            break
        offset = next_offset

# ---------------------------------------------------------------------------
# CATALOG MAP CACHE
# ---------------------------------------------------------------------------

_map_cache:     dict | None = None
_map_computing: bool        = False

def _build_map_from_payload() -> dict:
    """Fast catalog map: read the px/py that `quickbeam build --umap` baked into
    each point's payload, instead of recomputing UMAP. Pulls no vectors and runs
    no UMAP, so it scales to multi-million-point collections where the recompute
    path (all vectors in RAM + UMAP) is infeasible. The baked coords are already
    normalized to [-1, 1], so no rescaling is needed."""
    import numpy as np
    tracks:        list = []
    tag_positions: dict = {}
    for pt_id, payload, _ in _scroll_all(with_vectors=False):
        px, py = payload.get("px"), payload.get("py")
        if px is None or py is None:
            continue
        fields = payload.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        primary   = _role_tag_field_values(fields, 0)
        secondary = _role_tag_field_values(fields, 1)
        px, py    = float(px), float(py)
        tracks.append({
            "id":     str(pt_id),
            "title":  _role_title(fields, fallback=str(pt_id)),
            "artist": _role_subtitle(fields),
            "genres": primary[:3],
            "moods":  secondary[:3],
            "px":     px,
            "py":     py,
        })
        if primary:
            tag_positions.setdefault(primary[0], []).append((px, py))

    tag_centroids = []
    for tag, pts in tag_positions.items():
        if len(pts) < 5:
            continue
        a = np.array(pts)
        tag_centroids.append({"genre": tag, "px": float(a[:, 0].mean()),
                              "py": float(a[:, 1].mean()), "count": len(pts)})
    tag_centroids.sort(key=lambda x: -x["count"])
    print(f"[catalog/map] served {len(tracks)} tracks from baked px/py (no recompute)")
    return {"tracks": tracks, "total": len(tracks), "genres": tag_centroids[:40]}


def _build_map_sync() -> dict:
    total = _collection_count()
    print(f"[catalog/map] building projection for {total} tracks …")
    if total == 0:
        return {"tracks": [], "total": 0, "genres": []}

    _ensure_role_map()

    # ── Fast path: px/py baked into payloads by the builder (quickbeam build --umap
    #    / --umap-only). Read them directly — the only viable path at large scale.
    try:
        probe, _ = qdrant_client.scroll(
            collection_name=cfg.collection, limit=1,
            with_payload=True, with_vectors=False)
    except Exception:
        probe = None
    if (probe and isinstance(probe[0].payload, dict)
            and probe[0].payload.get("px") is not None
            and probe[0].payload.get("py") is not None):
        return _build_map_from_payload()

    # ── Slow path: recompute UMAP from vectors. Feasible only for small
    #    collections — large ones must bake px/py with the builder first.
    try:
        import umap as umap_lib
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("umap-learn is not installed. Run: pip install umap-learn") from exc

    embeddings, payloads, ids = [], [], []
    for pt_id, payload, vec in _scroll_all(with_vectors=True):
        fields = payload.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        embeddings.append(vec)
        payloads.append(fields)
        ids.append(str(pt_id))

    if not embeddings:
        return {"tracks": [], "total": 0, "genres": []}

    import numpy as np
    n = len(payloads)

    proj = None
    if _rm().get("spatial"):
        xy    = [_role_spatial_xy(p) for p in payloads]
        valid = sum(1 for c in xy if c is not None)
        if valid >= max(1, n // 2):
            proj = np.array([c if c is not None else (0.0, 0.0) for c in xy], dtype=np.float32)
            print(f"[catalog/map] using spatial role '{_rm()['spatial']}' for {valid}/{n} points")

    if proj is None:
        # Filter out None vectors before stacking
        valid_pairs = [(e, p, i) for e, p, i in zip(embeddings, payloads, ids) if e is not None]
        if not valid_pairs:
            return {"tracks": [], "total": 0, "genres": []}
        embeddings, payloads, ids = zip(*valid_pairs)
        emb_np = np.array(embeddings, dtype=np.float32)
        n = len(payloads)
        print(f"[catalog/map] running UMAP on {n} × {emb_np.shape[1]} matrix …")
        reducer = umap_lib.UMAP(
            n_components=2,
            n_neighbors=min(15, n - 1),
            min_dist=0.05,
            metric="cosine",
            random_state=42,
            low_memory=True,
            verbose=False,
        )
        proj = reducer.fit_transform(emb_np)

    for axis in range(2):
        mn, mx = float(proj[:, axis].min()), float(proj[:, axis].max())
        rng = mx - mn if mx != mn else 1.0
        proj[:, axis] = (proj[:, axis] - mn) / rng * 2 - 1

    tracks:        list = []
    tag_positions: dict = {}

    for payload, doc_id, pos in zip(payloads, ids, proj):
        primary   = _role_tag_field_values(payload, 0)
        secondary = _role_tag_field_values(payload, 1)
        px, py    = float(pos[0]), float(pos[1])
        tracks.append({
            "id":     doc_id,
            "title":  _role_title(payload, fallback=doc_id),
            "artist": _role_subtitle(payload),
            "genres": primary[:3],
            "moods":  secondary[:3],
            "px":     px,
            "py":     py,
        })
        if primary:
            tag_positions.setdefault(primary[0], []).append((px, py))

    import numpy as np
    tag_centroids = []
    for tag, pts in tag_positions.items():
        if len(pts) < 5:
            continue
        arr = np.array(pts)
        tag_centroids.append({
            "genre": tag,
            "px":    float(arr[:, 0].mean()),
            "py":    float(arr[:, 1].mean()),
            "count": len(pts),
        })
    tag_centroids.sort(key=lambda x: -x["count"])

    print(f"[catalog/map] done — {len(tracks)} items, {len(tag_centroids)} tag labels")
    return {"tracks": tracks, "total": len(tracks), "genres": tag_centroids[:40]}

# ---------------------------------------------------------------------------
# TEXT (LEXICAL) SEARCH
# ---------------------------------------------------------------------------

_text_index:      list[dict] | None = None
_text_index_lock = threading.Lock()

# True once background warmup has finished: lexical index built, embedding model
# loaded, and the vector index paged into memory. The /ready boot gate keys off
# this so the loading screen can hold until the very first query is fast.
_warm = False

# Real, not-cosmetic warmup progress for the boot splash. The dominant warmup cost
# is the lexical build, which scans a *known* number of records — so the splash can
# show `_warm_indexed / _warm_total` as a literal count of work completed, not an
# estimate. The two tail steps (embedding-model load, vector-index touch) have no
# measurable sub-progress; `_warm_phase` names the current step so the UI can show
# an honest indeterminate state for those instead of fabricating a percentage.
_warm_phase   = "starting"
_warm_indexed = 0
_warm_total   = 0

def _build_text_index_sync() -> list[dict]:
    global _warm_indexed, _warm_total
    total = _collection_count()
    if total == 0:
        return []
    # Publish the denominator before the scroll starts so the splash bar is a real
    # fraction (records folded in / total) from the first tick onward.
    _warm_total   = total
    _warm_indexed = 0
    print(f"[search/text] building lexical index over {total} records…")
    _ensure_role_map()
    out: list[dict] = []
    # Larger scroll pages → far fewer gRPC round-trips over the full collection,
    # which is the slow part of the cold build.
    for pt_id, payload, _ in _scroll_all(with_vectors=False, batch=20_000):
        fields   = payload.get("fields", {}) or {}
        owner    = payload.get("owner")
        subtitle = _role_subtitle(fields)
        title    = _role_title(fields)
        tags     = _role_tags(fields)
        out.append({
            "id":       str(pt_id),
            "owner":    owner,
            "fields":   fields,
            "_sub_l":   subtitle.lower(),
            "_title_l": title.lower(),
            "_tags_l":  " ".join(t.lower() for t in tags),
            "_sort":    (subtitle.lower(), title.lower()),
        })
        # Coarse progress tick — every 5k records is smooth enough for the bar
        # without churning a global per row over 860k iterations.
        if len(out) % 5_000 == 0:
            _warm_indexed = len(out)
    _warm_indexed = len(out)
    print(f"[search/text] built lexical index: {len(out)} records")
    return out

def _score_rec(rec: dict, q_l: str, tokens: list[str]) -> float:
    a, t, tags = rec["_sub_l"], rec["_title_l"], rec["_tags_l"]
    score = 0.0
    if   a == q_l:          score += 1000
    elif a.startswith(q_l): score += 400
    elif q_l and q_l in a:  score += 220
    if   t == q_l:          score += 350
    elif t.startswith(q_l): score += 160
    elif q_l and q_l in t:  score += 120
    if tokens:
        hay = f"{a} {t}"
        score += 60 * (sum(1 for tok in tokens if tok in hay) / len(tokens))
        score += 12 * (sum(1 for tok in tokens if tok in tags) / len(tokens))
    return score

def _ensure_text_index() -> list[dict]:
    """Build the lexical index once, lazily, under a lock + double-check so two
    concurrent callers (each on its own executor thread) don't both scroll the
    full 860k-record collection. Pre-called from warmup so the user's first text
    search does only scoring, not the build."""
    global _text_index
    if _text_index is None:
        with _text_index_lock:
            if _text_index is None:
                _text_index = _build_text_index_sync()
    return _text_index

def _search_text_sync(q: str, limit: int, owner: str | None) -> list[dict]:
    index  = _ensure_text_index()
    q_l    = q.strip().lower()
    tokens = [tok for tok in q_l.split() if tok]
    recs   = index if not owner else [r for r in index if r.get("owner") == owner]
    scored = [(s, r) for r in recs if (s := _score_rec(r, q_l, tokens)) > 0]
    scored.sort(key=lambda x: (-x[0], x[1]["_sort"]))
    return [{"id": r["id"], "fields": r["fields"], "owner": r.get("owner")}
            for _, r in scored[:limit]]

def _attach_embeddings(results: list[dict]) -> list[dict]:
    """Batch-fetch vectors for a result list so the frontend avoids a round-trip re-embed."""
    ids = [r["id"] for r in results]
    if not ids:
        return results
    # Qdrant retrieve by point IDs
    try:
        pts = qdrant_client.retrieve(
            collection_name=cfg.collection,
            ids=ids,
            with_vectors=True,
        )
        emap = {}
        for pt in pts:
            vec = pt.vector
            if vec is not None:
                emap[str(pt.id)] = vec if isinstance(vec, list) else vec.tolist()
        for r in results:
            e = emap.get(r["id"])
            if e is not None:
                r["embedding"] = e
    except Exception as exc:
        print(f"[_attach_embeddings] warn: {exc}")
    return results

# ---------------------------------------------------------------------------
# CHECKPOINT
# ---------------------------------------------------------------------------

def _load_checkpoint() -> dict:
    try:
        with open(cfg.checkpoint_file) as f:
            ckpt = json.load(f)
            # Normalise legacy flat format
            if "manifests" not in ckpt:
                ckpt = {"manifests": ckpt, "processed_track_ids": []}
            return ckpt
    except (FileNotFoundError, json.JSONDecodeError):
        return {"manifests": {}, "processed_track_ids": []}

def _save_checkpoint(checkpoint: dict) -> None:
    os.makedirs(os.path.dirname(cfg.checkpoint_file) or ".", exist_ok=True)
    with open(cfg.checkpoint_file, "w") as f:
        json.dump(checkpoint, f)

def _clear_checkpoint() -> None:
    try:
        os.remove(cfg.checkpoint_file)
    except FileNotFoundError:
        pass

# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------

class EmbedRequest(BaseModel):
    text:  str | None       = None
    texts: list[str] | None = None

class VectorSearchRequest(BaseModel):
    embedding: list[float]
    n_results: int        = 20
    owner:     str | None = None

class TextSearchRequest(BaseModel):
    q:     str
    limit: int        = 60
    owner: str | None = None

class BundlePoint(BaseModel):
    """A single pre-embedded point from a client bundle download."""
    track_id:  str
    fields:    dict
    embedding: list[float]
    owner:     str | None = None
    meta:      dict       = {}

class BundleUpsertRequest(BaseModel):
    points: list[BundlePoint]

# ---------------------------------------------------------------------------
# SUBGRAPH
# ---------------------------------------------------------------------------

PUBLISHES_QUERY = """
query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!) {
  manifestPublisheds(
    where: { schemaId: $schemaId }
    first: $first skip: $skip
    orderBy: blockNumber orderDirection: asc
  ) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash }
}
"""

UPDATES_QUERY = """
query Updates($schemaId: Bytes!, $first: Int!, $skip: Int!) {
  manifestUpdateds(
    where: { schemaId: $schemaId }
    first: $first skip: $skip
    orderBy: version orderDirection: desc
  ) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash }
}
"""

_SUBGRAPH_MAX_RETRIES    = 5
_SUBGRAPH_RETRY_STATUSES = {429, 500, 502, 503, 504}

async def _query_subgraph_async(session: aiohttp.ClientSession, query: str, variables: dict) -> dict:
    headers = {}
    if cfg.graph_api_key:
        headers["Authorization"] = f"Bearer {cfg.graph_api_key}"
    body      = {"query": query, "variables": variables}
    last_exc: Exception | None = None
    for attempt in range(_SUBGRAPH_MAX_RETRIES):
        try:
            async with session.post(
                cfg.subgraph_url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in _SUBGRAPH_RETRY_STATUSES:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history,
                        status=resp.status, message=f"retryable status {resp.status}",
                    )
                resp.raise_for_status()
                data = await resp.json()
                if "errors" in data:
                    raise RuntimeError(f"Subgraph error: {data['errors']}")
                return data["data"]
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if isinstance(exc, aiohttp.ClientResponseError) and exc.status not in _SUBGRAPH_RETRY_STATUSES:
                raise
            last_exc = exc
            if attempt < _SUBGRAPH_MAX_RETRIES - 1:
                backoff = min(8.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.3)
                print(f"  [subgraph] {type(exc).__name__} — retry {attempt + 1}/{_SUBGRAPH_MAX_RETRIES - 1} in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                continue
            raise
    raise last_exc if last_exc else RuntimeError("subgraph query failed")

async def _fetch_all_events_async(session: aiohttp.ClientSession, schema_id: str) -> tuple[list, list]:
    publishes, updates = [], []
    for target, query, key in [(publishes, PUBLISHES_QUERY, "manifestPublisheds"), (updates, UPDATES_QUERY, "manifestUpdateds")]:
        skip = 0
        while True:
            data  = await _query_subgraph_async(session, query, {"schemaId": schema_id, "first": cfg.page_size, "skip": skip})
            batch = data.get(key, [])
            target.extend(batch)
            if len(batch) < cfg.page_size:
                break
            skip += cfg.page_size
    return publishes, updates

# ---------------------------------------------------------------------------
# IPFS
# ---------------------------------------------------------------------------

FALLBACK_GATEWAYS = [
    "https://cloudflare-ipfs.com/ipfs",
    "https://ipfs.io/ipfs",
    "https://w3s.link/ipfs",
    "https://dweb.link/ipfs",
]

async def _fetch_json(session: aiohttp.ClientSession, sem: asyncio.Semaphore, cid: str) -> tuple[str, any]:
    async with sem:
        gateways = [cfg.ipfs_gateway.rstrip("/")] + FALLBACK_GATEWAYS
        for index, base_url in enumerate(gateways):
            url = f"{base_url}/{cid}"
            if index > 0:
                await asyncio.sleep(0.25 * index)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=cfg.ipfs_timeout)) as resp:
                    if resp.status == 429:
                        continue
                    resp.raise_for_status()
                    return cid, json.loads(await resp.text())
            except Exception as e:
                if index == len(gateways) - 1:
                    print(f"  [error] All gateways exhausted for CID {cid[:20]}…: {e}")
                continue
        return cid, None

async def fetch_all_ipfs(cids: list[str]) -> dict[str, any]:
    if not cids:
        return {}
    sem = asyncio.Semaphore(cfg.concurrency)
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*[_fetch_json(session, sem, cid) for cid in cids])
    return dict(results)

# ---------------------------------------------------------------------------
# SCHEMA FETCH + PAYLOAD RESOLUTION
# ---------------------------------------------------------------------------

def _build_seen_cids(publishes: list, updates: list) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    for p in publishes:
        cid, ts = p["manifestCid"], int(p["blockTimestamp"])
        if cid not in seen or ts > seen[cid]["blockTimestamp"]:
            seen[cid] = {"owner": p["owner"], "nameHash": p["nameHash"], "name": p["name"],
                         "manifestCid": cid, "version": 0, "blockTimestamp": ts}
    for u in updates:
        cid, ts = u["manifestCid"], int(u["blockTimestamp"])
        if cid not in seen or ts > seen[cid]["blockTimestamp"]:
            seen[cid] = {"owner": u["owner"], "nameHash": u["nameHash"], "name": u.get("name", u["nameHash"]),
                         "manifestCid": cid, "version": int(u["version"]), "blockTimestamp": ts}
    return seen

def _dedup_entries(all_entries: list) -> list:
    best:   dict[str, dict] = {}
    no_key: list[dict]      = []
    for item in all_entries:
        key = _track_id_str(item["entry"])
        if not key:
            no_key.append(item)
            continue
        ts = item["meta"]["blockTimestamp"]
        if key not in best or ts > best[key]["meta"]["blockTimestamp"]:
            best[key] = item
    return list(best.values()) + no_key

async def fetch_schema_entries(
    session: aiohttp.ClientSession,
    schema_name: str,
    schema_id:   str,
    prior_checkpoint: dict[str, int],
) -> tuple[list[dict], dict[str, int]]:
    publishes, updates = await _fetch_all_events_async(session, schema_id)
    print(f"  [{schema_name}] subgraph: {len(publishes)} publishes, {len(updates)} updates")

    seen_cids = _build_seen_cids(publishes, updates)
    new_cids  = [cid for cid, meta in seen_cids.items()
                 if prior_checkpoint.get(cid, -1) < meta["blockTimestamp"]]
    print(f"  [{schema_name}] IPFS: {len(new_cids)} manifests to fetch, {len(seen_cids) - len(new_cids)} skipped")

    fresh_manifests = await fetch_all_ipfs(new_cids)

    chunk_refs: list[dict] = []
    for cid, manifest_json in fresh_manifests.items():
        if not manifest_json:
            continue
        meta    = seen_cids[cid]
        entries = manifest_json.get("entries", [])
        for entry in entries:
            fields = entry.get("fields", {}) or {}
            dcid   = fields.get("dataCid")
            if dcid and isinstance(dcid, str):
                chunk_refs.append({"dataCid": dcid, "meta": meta})

    unique_data_cids = list({ref["dataCid"] for ref in chunk_refs})
    print(f"  [{schema_name}] deep IPFS: resolving {len(unique_data_cids)} chunk files…")

    resolved_payloads = await fetch_all_ipfs(unique_data_cids)
    failed_cids       = [c for c, p in resolved_payloads.items() if p is None]
    if failed_cids:
        print(f"  [retry] retrying {len(failed_cids)} failed chunks…")
        await asyncio.sleep(1.5)
        retry = await fetch_all_ipfs(failed_cids)
        resolved_payloads.update({k: v for k, v in retry.items() if v is not None})

    all_entries: list[dict] = []
    for ref in chunk_refs:
        dcid    = ref["dataCid"]
        meta    = ref["meta"]
        payload = resolved_payloads.get(dcid)
        if not payload:
            continue
        records: list = []
        if isinstance(payload, list):
            records = [r for r in payload if isinstance(r, dict)]
        elif isinstance(payload, dict):
            if "track" in payload and isinstance(payload["track"], dict):
                records = [{"fields": payload["track"]}]
            elif "fields" in payload and isinstance(payload["fields"], dict):
                records = [payload]
            else:
                records = [{"fields": payload}]
        for record in records:
            entry  = record if "fields" in record and isinstance(record["fields"], dict) else {"name": record.get("name", ""), "fields": record}
            fields = entry.get("fields", {}) or {}
            for pk in ["trackId", "track_id", "id", "contentId"]:
                if fields.get(pk):
                    fields["trackId"] = str(fields[pk]).strip().removeprefix("track:")
                    break
            entry["fields"] = fields
            all_entries.append({"entry": entry, "meta": meta})

    new_checkpoint: dict[str, int] = dict(prior_checkpoint)
    for cid, meta in seen_cids.items():
        new_checkpoint[cid] = meta["blockTimestamp"]

    deduped = _dedup_entries(all_entries)
    print(f"  [{schema_name}] {len(deduped)} active records")
    return deduped, new_checkpoint

# ---------------------------------------------------------------------------
# JOIN + DOCUMENT BUILD
# ---------------------------------------------------------------------------

def _flatten_fields(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if isinstance(v, dict) and v.get("@type") == "handle":
            out[k] = v.get("uri", "")
        else:
            out[k] = v
    return out

TAG_FIELDS = {"genres", "moods", "themes", "contexts"}

def _is_auto_searchable() -> bool:
    return cfg.searchable_fields.strip().lower() == "auto"

def _build_searchable_text(fields: dict) -> str:
    if not _is_auto_searchable():
        searchable = get_searchable_fields()
        tag_terms, other_terms = [], []
        for key in searchable:
            val    = fields.get(key)
            bucket = tag_terms if key in TAG_FIELDS else other_terms
            if isinstance(val, list):
                bucket.extend(str(v) for v in val if v)
            elif val:
                bucket.append(str(val))
        parts = []
        if tag_terms:
            tag_text = " ".join(tag_terms)
            parts.append(tag_text); parts.append(tag_text)
        parts.extend(other_terms)
        return " ".join(parts) or "record"

    tags       = _role_tags(fields)
    title      = _role_title(fields)
    subtitle   = _role_subtitle(fields)
    text_terms = [str(fields[t]) for t in (_rm().get("text", []) or []) if fields.get(t)]
    parts: list[str] = []
    if tags:
        tag_text = " ".join(tags)
        parts.append(tag_text); parts.append(tag_text)
    if title:    parts.append(title)
    if subtitle: parts.append(subtitle)
    parts.extend(text_terms)
    return " ".join(parts).strip() or title or subtitle or "record"

def join_records(entries_by_schema: dict[str, list[dict]], primary: str) -> list[dict]:
    primary_entries = entries_by_schema.get(primary, [])
    if not primary_entries:
        print(f"  [warn] primary schema '{primary}' has no entries")
        return []

    secondary_fields: dict[str, dict] = {}
    for schema_name, entries in entries_by_schema.items():
        if schema_name == primary:
            continue
        for item in entries:
            key = _track_id_str(item["entry"])
            if not key:
                continue
            flat = _flatten_fields(item["entry"].get("fields", {}))
            secondary_fields.setdefault(key, {}).update(flat)

    global debug_secondary
    debug_secondary = secondary_fields

    merged: list[dict] = []
    for item in primary_entries:
        track_id = _track_id_str(item["entry"])
        if not track_id:
            continue
        core_fields = _flatten_fields(item["entry"].get("fields", {}))
        tag_fields  = secondary_fields.get(track_id, {})
        fields      = {**core_fields, **tag_fields}
        fields["trackId"] = track_id
        merged.append({"track_id": track_id, "fields": fields, "meta": item["meta"], "has_tags": bool(tag_fields)})

    global role_map_global
    role_map_global = infer_roles([m["fields"] for m in merged])
    print(f"  [roles] inferred: {role_map_global.get('labels')}")

    joined  = []
    matched = 0
    for m in merged:
        track_id, fields = m["track_id"], m["fields"]
        doc = _build_searchable_text(fields)
        if m["has_tags"]:
            matched += 1
        joined.append({
            "id":      track_id,          # string — will be converted to UUID on upsert
            "doc":     doc,
            "fields":  fields,
            "owner":   m["meta"]["owner"],
            "meta":    m["meta"],
        })

    print(f"  [join] {len(joined)} records — {matched} with tags")
    return joined

# ---------------------------------------------------------------------------
# EMBED + UPSERT (server-side ingestion)
# ---------------------------------------------------------------------------

def _truncate_normalize(vec: list[float], dim: int) -> list[float]:
    """Matryoshka truncation. nomic-embed-text-v1.5 is trained so the leading
    components of its 768-dim output form valid lower-dim embeddings; slice to
    `dim` and re-normalize so query vectors line up with a snapshot built at `dim`.
    """
    v = vec[:dim]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]

def _embed_texts(texts: list[str], prefix: str = "search_document") -> list[list[float]]:
    # nomic-embed-text-v1.5 is asymmetric: documents must be prefixed
    # "search_document:" (ingestion default), queries "search_query:". Passing
    # the right prefix on the query side measurably improves retrieval.
    prefixed = [f"{prefix}: {t}" for t in texts]
    vectors  = []
    for vec in embed_engine.embed(prefixed, batch_size=64):
        v = vec.tolist() if not isinstance(vec, list) else vec
        if vector_dim and len(v) > vector_dim:
            v = _truncate_normalize(v, vector_dim)
        vectors.append(v)
    return vectors

def _upsert_joined(joined: list[dict]) -> None:
    """Embed and upsert a list of joined records into Qdrant."""
    BATCH = 2000
    for i in range(0, len(joined), BATCH):
        chunk   = joined[i:i + BATCH]
        texts   = [r["doc"] for r in chunk]
        vectors = _embed_texts(texts)
        points  = [
            qmodels.PointStruct(
                id      = _str_to_uuid(r["id"]),
                vector  = vec,
                payload = {
                    "id":     r["id"],
                    "owner":  r["owner"],
                    "fields": r["fields"],
                    "meta":   r["meta"],
                },
            )
            for r, vec in zip(chunk, vectors)
        ]
        qdrant_client.upsert(collection_name=cfg.collection, points=points, wait=True)
        print(f"  upserted {min(i + BATCH, len(joined))}/{len(joined)}")

# ---------------------------------------------------------------------------
# INGESTION
# ---------------------------------------------------------------------------

async def ingest():
    global _map_cache, _text_index
    loop       = asyncio.get_event_loop()
    schemas    = get_schemas()
    primary    = get_primary(schemas)
    checkpoint = _load_checkpoint()

    print(f"\n{'=' * 60}")
    print(f"Ingesting {len(schemas)} schema(s): {list(schemas.keys())}")
    print(f"{'=' * 60}")

    connector = aiohttp.TCPConnector(limit=16, limit_per_host=8, enable_cleanup_closed=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*[
            fetch_schema_entries(session, name, schema_id, checkpoint.get("manifests", {}).get(name, {}))
            for name, schema_id in schemas.items()
        ])

    entries_by_schema:         dict[str, list[dict]]     = {}
    new_checkpoints_by_schema: dict[str, dict[str, int]] = {}
    for (name, _), (entries, new_ckpt) in zip(schemas.items(), results):
        entries_by_schema[name]         = entries
        new_checkpoints_by_schema[name] = new_ckpt

    joined = join_records(entries_by_schema, primary)

    if not joined:
        print("  No records to update.")
        for name, ckpt in new_checkpoints_by_schema.items():
            checkpoint.setdefault("manifests", {})[name] = ckpt
        _save_checkpoint(checkpoint)
        return

    processed_ids = set(checkpoint.get("processed_track_ids", []))
    new_records   = [r for r in joined if r["id"] not in processed_ids]
    print(f"  Diff: {len(new_records)} new, {len(joined) - len(new_records)} already processed")

    if new_records:
        await loop.run_in_executor(None, _upsert_joined, new_records)
        checkpoint.setdefault("processed_track_ids", []).extend(r["id"] for r in new_records)

    for name, ckpt in new_checkpoints_by_schema.items():
        checkpoint.setdefault("manifests", {})[name] = ckpt
    _save_checkpoint(checkpoint)

    _map_cache  = None
    _text_index = None

    print(f"Ingestion complete. Collection contains {_collection_count()} points.")

# ---------------------------------------------------------------------------
# RESPONSE HELPERS
# ---------------------------------------------------------------------------

def _hit_from_point(pt, score: float | None = None) -> dict:
    """Normalise a Qdrant ScoredPoint or Record into the legacy hit shape."""
    payload = pt.payload or {}
    fields  = payload.get("fields", {})
    owner   = payload.get("owner")
    meta    = payload.get("meta", {}) or {}
    hit     = {"id": payload.get("id", str(pt.id)), "fields": fields, "owner": owner}
    # Surface on-chain provenance so the MCP layer can attach it to every result.
    hit["meta"] = {
        "manifestCid":    meta.get("manifestCid"),
        "blockTimestamp": meta.get("blockTimestamp"),
        "version":        meta.get("version"),
        "owner":          meta.get("owner"),
    }
    if score is not None:
        hit["score"] = round(score, 4)
    vec = getattr(pt, "vector", None)
    if vec is not None:
        hit["embedding"] = vec if isinstance(vec, list) else vec.tolist()
    return hit

# ---------------------------------------------------------------------------
# BUNDLE SEED FROM IPFS
# ---------------------------------------------------------------------------

async def _seed_from_ipfs(cid: str) -> None:
    """
    Fetch an NDJSON bundle from IPFS and upsert it into the local collection.
    Runs as a background task at startup — server is live and queryable while
    this progresses. Tries the configured gateway first, then fallbacks.
    """
    global _map_cache, _text_index

    gateways = [cfg.ipfs_gateway.rstrip("/")] + FALLBACK_GATEWAYS
    resp     = None

    async with aiohttp.ClientSession() as session:
        for i, base in enumerate(gateways):
            url = f"{base}/{cid}"
            print(f"[seed] fetching bundle from {url}")
            try:
                resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=None))
                if resp.status == 200:
                    break
                print(f"[seed] gateway returned {resp.status}, trying next…")
                resp = None
            except Exception as exc:
                print(f"[seed] gateway error: {exc}, trying next…")

        if resp is None:
            print(f"[seed] all gateways failed for CID {cid} — aborting seed")
            return

        BATCH    = 500
        batch:   list[qmodels.PointStruct] = []
        total    = 0
        loop     = asyncio.get_event_loop()

        async def _flush(b):
            await loop.run_in_executor(
                None,
                lambda: qdrant_client.upsert(collection_name=cfg.collection, points=b, wait=True),
            )

        async for raw in resp.content:
            for line in raw.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                track_id = row.get("track_id", "")
                vec      = row.get("embedding")
                if not track_id or not vec:
                    continue
                batch.append(qmodels.PointStruct(
                    id      = _str_to_uuid(track_id),
                    vector  = vec,
                    payload = {
                        "id":     track_id,
                        "owner":  row.get("owner"),
                        "fields": row.get("fields", {}),
                        "meta":   row.get("meta", {}),
                    },
                ))
                if len(batch) >= BATCH:
                    await _flush(batch)
                    total += len(batch)
                    batch  = []
                    print(f"[seed] {total} points upserted…", end="\r", flush=True)

        if batch:
            await _flush(batch)
            total += len(batch)

    _map_cache  = None
    _text_index = None
    print(f"\n[seed] done — {total} points seeded from {cid}")

# ---------------------------------------------------------------------------
# APP LIFECYCLE
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global qdrant_client, embed_engine, vector_dim, _warm
    # Silence the boot-poll access logs (main polls /ready every ~300ms until the
    # warmup below flips it to 200). Done here, after uvicorn has set up its
    # loggers, so the filter sticks.
    logging.getLogger("uvicorn.access").addFilter(_AccessLogPollFilter())
    # `timeout` is the per-request gRPC deadline. The default (5s) is too tight
    # for a cold first query: against a freshly-recovered 1M-vector collection,
    # Qdrant has to page the HNSW graph + mmap'd segments into memory before it
    # can answer, which can take far longer than 5s. Give it real headroom — a
    # warm query still returns in milliseconds, so this only bites on cold start.
    if cfg.qdrant_url:
        qdrant_client = QdrantClient(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key,
            prefer_grpc=True,
            timeout=120,
        )
        print(f"[startup] connecting to Qdrant Cloud: {cfg.qdrant_url}")
    else:
        qdrant_client = QdrantClient(
            host=cfg.qdrant_host,
            port=cfg.qdrant_port,
            grpc_port=cfg.qdrant_grpc_port,
            prefer_grpc=True,
            timeout=120,
        )
        print(f"[startup] connecting to local Qdrant: {cfg.qdrant_host}:{cfg.qdrant_port}")

    if cfg.reset and qdrant_client.collection_exists(cfg.collection):
        print("[startup] resetting collection…")
        qdrant_client.delete_collection(cfg.collection)
        _clear_checkpoint()

    # Read-only path: the snapshot has already been recovered into Qdrant (see
    # ensureCollection in main), so the collection — and its true vector dim —
    # already exist. Trust that dim and truncate query embeddings to match it,
    # rather than guessing from the model. Only create a collection when one is
    # genuinely missing (e.g. standalone --bundle-cid seeding).
    if qdrant_client.collection_exists(cfg.collection):
        info       = qdrant_client.get_collection(cfg.collection)
        vparams    = info.config.params.vectors
        snap_dim   = getattr(vparams, "size", None)
        vector_dim = cfg.dim or snap_dim or MODEL_DIM_MAP.get(cfg.embedding_model, 768)
        if cfg.dim and snap_dim and cfg.dim != snap_dim:
            print(f"[startup] WARNING: --dim {cfg.dim} != collection dim {snap_dim}; using {vector_dim}")
        print(f"[startup] collection '{cfg.collection}' present — vector dim = {vector_dim}")
    else:
        vector_dim = cfg.dim or MODEL_DIM_MAP.get(cfg.embedding_model, 768)
        qdrant_client.create_collection(
            cfg.collection,
            vectors_config=qmodels.VectorParams(size=vector_dim, distance=qmodels.Distance.COSINE),
        )
        print(f"[startup] created collection '{cfg.collection}' dim={vector_dim}")

    print(f"[startup] loading embedding model: {cfg.embedding_model}")
    embed_engine = TextEmbedding(model_name=cfg.embedding_model, max_length=512)

    count = _collection_count()
    print(f"[startup] collection '{cfg.collection}' ready — {count} points")

    if count == 0 and cfg.bundle_cid:
        print(f"[startup] collection empty — seeding from bundle CID {cfg.bundle_cid}")
        asyncio.create_task(_seed_from_ipfs(cfg.bundle_cid))
    elif cfg.bundle_cid:
        print(f"[startup] --bundle-cid provided but collection already has {count} points — skipping seed")
    else:
        print(f"[startup] use POST /reingest to pull new subgraph data, POST /bundle/upsert to seed from a bundle")

    # Warm both hot paths in the background so the user's first query is fast:
    # fastembed finishes loading its ONNX model lazily on the first embed, and
    # Qdrant pages the index into memory on the first search. Doing this off the
    # critical path means /health comes up immediately while warmup proceeds.
    if count > 0:
        asyncio.create_task(_warmup())
    else:
        # Nothing to warm (empty / still-seeding collection) — don't gate boot on
        # a warmup that will never run.
        _warm = True

    yield


async def _warmup() -> None:
    global _warm, _warm_phase
    loop = asyncio.get_event_loop()
    try:
        # Pre-build the lexical index first: it's the first thing most users hit,
        # and building it lazily on the first /search/text means an 860k-record
        # scroll blocks that query. Doing it here (behind /health, at startup)
        # makes the first text search do only scoring. This is the long phase and
        # the one with a real percentage (records scanned / total).
        _warm_phase = "building text index"
        await loop.run_in_executor(None, _ensure_text_index)
        # Force the embedding model to fully initialise (semantic search path).
        # No measurable sub-progress — the UI shows this as an indeterminate step.
        _warm_phase = "loading embedding model"
        await loop.run_in_executor(None, lambda: _embed_texts(["warmup"]))
        # Touch the collection so its HNSW graph + segments page into memory.
        _warm_phase = "warming vector index"
        dim = vector_dim or MODEL_DIM_MAP.get(cfg.embedding_model, 768)
        await loop.run_in_executor(None, lambda: qdrant_client.query_points(
            collection_name=cfg.collection,
            query=[0.0] * dim,
            limit=1,
            with_payload=False,
            with_vectors=False,
        ))
        print("[startup] warmup complete — lexical index + embeddings + vector index hot")
    except Exception as e:
        print(f"[startup] warmup skipped: {e}")
    finally:
        # Release the boot gate either way: on success everything's hot; on
        # failure the lazy fallbacks still work and we must not hang the splash.
        _warm_phase = "ready"
        _warm = True


class _AccessLogPollFilter(logging.Filter):
    """Drop uvicorn access-log lines for the boot-gate poll endpoints. While the
    splash waits for warmup, main hits /ready every ~300ms — without this the
    console fills with '"GET /ready" 503' until the index is hot."""
    _QUIET = ("/ready", "/health")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._QUIET)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.get("/browse")
async def browse(limit: int = Query(20), offset: int = Query(0)):
    loop = asyncio.get_event_loop()
    records, _ = await loop.run_in_executor(
        None,
        lambda: qdrant_client.scroll(
            collection_name=cfg.collection,
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        ),
    )
    hits  = [_hit_from_point(pt) for pt in records]
    total = _collection_count()
    return {"results": hits, "total": total}

@app.get("/search")
async def search(
    q:         str        = Query(...),
    n_results: int        = Query(10),
    owner:     str | None = Query(None),
):
    loop = asyncio.get_event_loop()

    # Embed the query text. nomic is asymmetric — use the query-side prefix.
    vectors = await loop.run_in_executor(None, lambda: _embed_texts([q], prefix="search_query"))
    query_vec = vectors[0]

    query_filter = qmodels.Filter(
        must=[qmodels.FieldCondition(key="owner", match=qmodels.MatchValue(value=owner))]
    ) if owner else None

    resp = await loop.run_in_executor(
        None,
        lambda: qdrant_client.query_points(
            collection_name=cfg.collection,
            query=query_vec,
            limit=n_results,
            query_filter=query_filter,
            with_payload=True,
            with_vectors=True,
        ),
    )
    return {"results": [_hit_from_point(pt, pt.score) for pt in resp.points]}

@app.post("/search/vector")
async def search_vector(body: VectorSearchRequest):
    loop         = asyncio.get_event_loop()
    query_filter = qmodels.Filter(
        must=[qmodels.FieldCondition(key="owner", match=qmodels.MatchValue(value=body.owner))]
    ) if body.owner else None
    resp = await loop.run_in_executor(
        None,
        lambda: qdrant_client.query_points(
            collection_name=cfg.collection,
            query=body.embedding,
            limit=body.n_results,
            query_filter=query_filter,
            with_payload=True,
            with_vectors=True,
        ),
    )
    return {"results": [_hit_from_point(pt, pt.score) for pt in resp.points]}

@app.post("/search/text")
async def search_text(body: TextSearchRequest):
    loop    = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _search_text_sync, body.q, body.limit, body.owner)
    results = await loop.run_in_executor(None, _attach_embeddings, results)
    return {"results": results}

@app.post("/embed")
async def embed_endpoint(body: EmbedRequest):
    loop = asyncio.get_event_loop()
    if body.texts:
        vectors = await loop.run_in_executor(None, lambda: _embed_texts(body.texts))
        return {"embeddings": vectors}
    if body.text:
        vectors = await loop.run_in_executor(None, lambda: _embed_texts([body.text]))
        return {"embedding": vectors[0]}
    return {"error": "provide 'text' or 'texts'"}

@app.post("/bundle/upsert")
async def bundle_upsert(body: BundleUpsertRequest):
    """
    Accept pre-embedded points from a client bundle download and upsert them
    directly — no re-embedding needed. This is the fast path for seeding a
    local instance from a distributed bundle.
    """
    if not body.points:
        return {"upserted": 0}

    loop   = asyncio.get_event_loop()
    points = [
        qmodels.PointStruct(
            id      = _str_to_uuid(p.track_id),
            vector  = p.embedding,
            payload = {
                "id":     p.track_id,
                "owner":  p.owner,
                "fields": p.fields,
                "meta":   p.meta,
            },
        )
        for p in body.points
    ]

    await loop.run_in_executor(
        None,
        lambda: qdrant_client.upsert(collection_name=cfg.collection, points=points, wait=True),
    )

    global _map_cache, _text_index
    _map_cache  = None
    _text_index = None

    return {"upserted": len(points)}

@app.post("/bundle/import")
async def bundle_import(request: Request):
    """
    Streaming NDJSON import — the natural counterpart to GET /bundle/export.
    Pipe directly:
      curl http://host-a:8080/bundle/export | curl -X POST http://host-b:8080/bundle/import --data-binary @-
    Upserts in batches of 500 as lines arrive; returns total count when done.
    """
    BATCH = 500
    batch:    list[qmodels.PointStruct] = []
    upserted  = 0
    loop      = asyncio.get_event_loop()

    async def _flush(b):
        await loop.run_in_executor(
            None,
            lambda: qdrant_client.upsert(collection_name=cfg.collection, points=b, wait=True),
        )

    async for raw_line in request.stream():
        for line in raw_line.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            track_id = row.get("track_id", "")
            vec      = row.get("embedding")
            if not track_id or not vec:
                continue
            batch.append(qmodels.PointStruct(
                id      = _str_to_uuid(track_id),
                vector  = vec,
                payload = {
                    "id":     track_id,
                    "owner":  row.get("owner"),
                    "fields": row.get("fields", {}),
                    "meta":   row.get("meta", {}),
                },
            ))
            if len(batch) >= BATCH:
                await _flush(batch)
                upserted += len(batch)
                batch = []

    if batch:
        await _flush(batch)
        upserted += len(batch)

    global _map_cache, _text_index
    _map_cache  = None
    _text_index = None

    return {"upserted": upserted}

@app.get("/bundle/export")
async def bundle_export(
    limit:  int        = Query(default=0,    description="Max points to export. 0 = all."),
    offset: int        = Query(default=0,    description="Skip this many points (for pagination)."),
    owner:  str | None = Query(default=None, description="Filter by owner address."),
):
    """
    Stream pre-embedded points as newline-delimited JSON (NDJSON).
    Each line is one point in BundlePoint format — no buffering.
    Pipe directly into /bundle/upsert via the companion import script,
    or collect with: curl ... | jq -s '{"points": .}' | curl -X POST .../bundle/upsert
    """
    query_filter = qmodels.Filter(
        must=[qmodels.FieldCondition(key="owner", match=qmodels.MatchValue(value=owner))]
    ) if owner else None

    def _stream():
        cursor    = None
        page      = 500        # smaller pages so first bytes arrive fast
        emitted   = 0
        skipped   = 0

        while True:
            batch, next_cursor = qdrant_client.scroll(
                collection_name=cfg.collection,
                scroll_filter=query_filter,
                limit=page,
                offset=cursor,
                with_payload=True,
                with_vectors=True,
            )
            for pt in batch:
                if offset and skipped < offset:
                    skipped += 1
                    continue
                payload = pt.payload or {}
                vec     = pt.vector
                if vec is None:
                    continue
                row = {
                    "track_id":  payload.get("id", str(pt.id)),
                    "fields":    payload.get("fields", {}),
                    "embedding": vec if isinstance(vec, list) else vec.tolist(),
                    "owner":     payload.get("owner"),
                    "meta":      payload.get("meta", {}),
                }
                yield json.dumps(row, separators=(",", ":")) + "\n"
                emitted += 1
                if limit and emitted >= limit:
                    return
            if next_cursor is None:
                break
            cursor = next_cursor

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"X-Collection": cfg.collection},
    )

def _schema_payload_sync() -> dict:
    rm        = _ensure_role_map()
    tag_fields = rm.get("tags", []) or []
    vocab: dict[str, set] = {f: set() for f in tag_fields}
    if tag_fields:
        try:
            for _, payload, _ in _scroll_all(with_vectors=False):
                fields = payload.get("fields", {}) or {}
                for f in tag_fields:
                    v = fields.get(f)
                    if isinstance(v, list):
                        vocab[f].update(str(x) for x in v if x)
        except Exception as e:
            print(f"[schema] facet scan failed: {e}")
    facets = {f: sorted(vals) for f, vals in vocab.items()}
    return {"roles": rm, "labels": rm.get("labels", {}), "facets": facets,
            "primary_tag": tag_fields[0] if tag_fields else None}

@app.get("/schema")
async def schema():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _schema_payload_sync)

@app.get("/catalog/map")
async def catalog_map():
    global _map_cache, _map_computing
    # Fast path: stream the prebuilt artifact (quickbeam build --umap-target file).
    # It is already gzipped JSON in the exact response shape, so we stream the
    # bytes straight from disk — no recompute, no loading 10M points into RAM.
    map_file = getattr(cfg, "catalog_map_file", None)
    if map_file and os.path.exists(map_file):
        def _iter():
            with open(map_file, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    yield chunk
        return StreamingResponse(
            _iter(),
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "X-Map-Source": "artifact"},
        )
    if _map_cache is not None:
        return _map_cache
    if _map_computing:
        return {"computing": True, "total": 0, "tracks": [], "genres": []}
    _map_computing = True
    loop = asyncio.get_event_loop()
    try:
        _map_cache = await loop.run_in_executor(None, _build_map_sync)
        return _map_cache
    except RuntimeError as e:
        return {"error": str(e), "total": 0, "tracks": [], "genres": []}
    except Exception as e:
        print(f"[catalog/map] error: {e}")
        return {"error": str(e), "total": 0, "tracks": [], "genres": []}
    finally:
        _map_computing = False

@app.post("/catalog/map/refresh")
async def catalog_map_refresh(background_tasks: BackgroundTasks):
    async def _refresh():
        global _map_cache, _map_computing
        _map_cache     = None
        _map_computing = True
        loop = asyncio.get_event_loop()
        try:
            _map_cache = await loop.run_in_executor(None, _build_map_sync)
        except Exception as e:
            print(f"[catalog/map/refresh] error: {e}")
        finally:
            _map_computing = False
    background_tasks.add_task(_refresh)
    return {"status": "refresh started", "note": "poll GET /catalog/map for progress"}

@app.get("/debug")
async def debug():
    all_ids, all_owners = [], []
    for _, payload, _ in _scroll_all(with_vectors=False):
        all_ids.append(payload.get("id", ""))
        all_owners.append(payload.get("owner", ""))
    primary_ids = sorted(all_ids)
    return {
        "primary_track_ids":    primary_ids,
        "secondary_tag_keys":   sorted(debug_secondary.keys()),
        "matched":              sorted(set(primary_ids) & set(debug_secondary.keys())),
        "unmatched_primary":    sorted(set(primary_ids) - set(debug_secondary.keys())),
        "unmatched_tags":       sorted(set(debug_secondary.keys()) - set(primary_ids)),
        "tag_details": {
            k: {
                "genres":   v.get("genres"),
                "moods":    v.get("moods"),
                "themes":   v.get("themes"),
                "contexts": v.get("contexts"),
            }
            for k, v in debug_secondary.items()
        },
    }

@app.get("/health")
async def health():
    # Read-only server: schemas/checkpoints belong to the offline builder, not here.
    # Roles are inferred from the recovered data on first access.
    return {
        "status":        "ok",
        "count":         _collection_count(),
        "collection":    cfg.collection,
        "dim":           vector_dim,
        "roles":         role_map_global,
        "map_cached":    _map_cache is not None,
        "map_computing": _map_computing,
        "text_indexed":  _text_index is not None,
        "warm":          _warm,
        "warm_phase":    _warm_phase,
        "warm_indexed":  _warm_indexed,
        "warm_total":    _warm_total,
    }

@app.get("/ready")
async def ready(response: Response):
    """Boot gate. Returns 200 only once background warmup has finished (lexical
    index + embedding model + vector index hot), so the loading screen can hold
    until the very first query is fast. 503 while still warming.

    The 503 body carries *real* progress: `phase` names the current step and,
    during the lexical build, `indexed`/`total`/`pct` report records scanned so
    far. `pct` is non-null only while that build runs — the tail steps have no
    measurable sub-progress, so the splash shows them as an indeterminate state
    rather than a fabricated number."""
    if _warm:
        return {"ready": True}
    response.status_code = 503
    building = _warm_phase == "building text index"
    pct = round(100 * _warm_indexed / _warm_total) if (building and _warm_total) else None
    return {
        "ready":   False,
        "phase":   _warm_phase,
        "indexed": _warm_indexed,
        "total":   _warm_total,
        "pct":     pct,
    }

@app.post("/reingest")
async def reingest():
    asyncio.create_task(ingest())
    return {"status": "accepted"}

@app.post("/reingest/full")
async def reingest_full():
    global _map_cache, _text_index
    _clear_checkpoint()
    _map_cache  = None
    _text_index = None
    asyncio.create_task(ingest())
    return {"status": "accepted", "note": "checkpoint cleared — full reingest in progress"}

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def _maybe_add_x402() -> None:
    """Install the x402 middleware when --x402-pay-to is configured."""
    if not getattr(cfg, "x402_pay_to", None):
        print("[startup] x402 disabled (no --x402-pay-to); search routes are free")
        return
    price_atomic = x402mod.price_to_atomic(cfg.x402_price, cfg.x402_decimals)
    config = x402mod.X402Config(
        pay_to=cfg.x402_pay_to,
        price_atomic=price_atomic,
        network=cfg.x402_network,
        asset=cfg.x402_asset or "",
        facilitator_url=cfg.x402_facilitator,
        description="quickbeam semantic search query",
    )
    app.add_middleware(x402mod.build_middleware(config), config=config)
    mode = "facilitator" if cfg.x402_facilitator else "local-verify"
    print(f"[startup] x402 ENABLED ({mode}) — {sorted(config.gated_paths)} "
          f"cost {cfg.x402_price} on {cfg.x402_network}, paid to {cfg.x402_pay_to}")


def main() -> None:
    global cfg
    cfg = parse_args()
    _maybe_add_x402()
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()