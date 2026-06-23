"""
Semantic CDN — distribute the public embeddings as static, pullable artifacts.

The Fangorn thesis is "knowledge is public, intent is private." The search server
(server.py) runs queries *server-side*, so the node sees every query vector. This
module inverts that: it BAKES the embedded graph into immutable, content-addressed
shard files (a "domain"), and SERVES them as plain static files. A client pulls a
domain into its own local Qdrant (see pull.py) and queries it offline — the CDN
observes nothing but which domains were downloaded.

Two entrypoints, both driven by their own argparse so the typer passthrough in
cli.py keeps their --help intact:

  bake_main()   `quickbeam cdn bake`   Qdrant collection -> ./cdn/<domain>/shard-*.ndjson.gz
  serve_main()  `quickbeam cdn serve`  ./cdn -> HTTP (FileResponse, Range-resumable)

A *domain* is operator-declared: a named filter over the source collection
(domains.json). Shards reuse the existing /bundle/export row shape verbatim
({track_id, fields, embedding, owner, meta}) so pull.py and /bundle/import agree.
"""
import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import time

from qdrant_client import QdrantClient
from qdrant_client import models as qmodels

from quickbeam.roles import infer_roles

# How many baked records to sample for per-domain role inference.
ROLE_SAMPLE_SIZE = 500


# ---------------------------------------------------------------------------
# SHARED — Qdrant client (mirrors server.py's cloud/local selection)
# ---------------------------------------------------------------------------
def _add_qdrant_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--qdrant-url", default=None, metavar="URL",
                        help="Qdrant Cloud URL (overrides --qdrant-host/port).")
    parser.add_argument("--qdrant-api-key", default=None, help="Qdrant Cloud API key.")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--qdrant-grpc-port", type=int, default=6334)


def make_qdrant(args) -> QdrantClient:
    if getattr(args, "qdrant_url", None):
        print(f"[cdn] connecting to Qdrant Cloud: {args.qdrant_url}")
        return QdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key,
                            prefer_grpc=True, timeout=120)
    print(f"[cdn] connecting to local Qdrant: {args.qdrant_host}:{args.qdrant_port}")
    return QdrantClient(host=args.qdrant_host, port=args.qdrant_port,
                        grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=120)


# ---------------------------------------------------------------------------
# DOMAIN FILTER — operator config dict -> Qdrant Filter
# ---------------------------------------------------------------------------
def _build_filter(spec: dict) -> qmodels.Filter | None:
    """Translate a domain's `filter` dict into a Qdrant Filter. Supported keys:
       entityType: [list]  -> payload `entityType` MatchAny
       owner:      [list]  -> payload `owner`      MatchAny
    Multiple keys are AND-ed (must). An empty/missing filter selects everything."""
    if not spec:
        return None
    must = []
    field_map = {"entityType": "entityType", "owner": "owner"}
    for key, payload_key in field_map.items():
        vals = spec.get(key)
        if not vals:
            continue
        if isinstance(vals, str):
            vals = [vals]
        must.append(qmodels.FieldCondition(
            key=payload_key, match=qmodels.MatchAny(any=list(vals))))
    return qmodels.Filter(must=must) if must else None


# ---------------------------------------------------------------------------
# BAKE
# ---------------------------------------------------------------------------
def _bake_args():
    p = argparse.ArgumentParser(
        prog="quickbeam cdn bake",
        description="Bake a Qdrant collection into immutable Semantic CDN shards.")
    p.add_argument("--config", default="domains.json",
                   help="Operator domain config (name -> filter). See module docs.")
    p.add_argument("--cdn-dir", default="./cdn", help="Output directory for baked shards.")
    p.add_argument("--collection", default="fangorn", help="Source Qdrant collection.")
    p.add_argument("--domain", default=None,
                   help="Bake only this domain (default: every domain in --config).")
    p.add_argument("--shard-size", type=int, default=50000,
                   help="Points per shard file.")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap total points baked per domain (0 = all). Use a small value "
                        "to bake a lightweight snapshot for in-browser clients.")
    p.add_argument("--scroll-batch", type=int, default=2000,
                   help="Qdrant scroll page size.")
    p.add_argument("--embedding-model", default="nomic-ai/nomic-embed-text-v1.5",
                   help="Recorded in the manifest (Qdrant doesn't store the model name).")
    _add_qdrant_args(p)
    return p.parse_args()


def _load_bundle_schema(path):
    """Load a Fangorn bundle schema JSON and return its {nodes, edges} block, or
    None. Baking this into the manifest lets an offline client render typed
    connections (the relationship vocabulary) without the live schema registry.

    Accepts either a full schema doc ({name, kind, bundle:{nodes,edges}}) or a
    bare {nodes, edges} block."""
    if not path:
        return None
    if not os.path.exists(path):
        print(f"[bake]   warning: bundle_schema not found: {path!r} (skipping)")
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[bake]   warning: could not read bundle_schema {path!r}: {e}")
        return None
    b = data.get("bundle", data)
    out = {}
    if isinstance(b.get("nodes"), (dict, list)):
        out["nodes"] = b["nodes"]
    if isinstance(b.get("edges"), list):
        out["edges"] = b["edges"]
    return out or None


def _shard_row(point) -> dict:
    """Reuse the /bundle/export shape so pull.py / /bundle/import agree."""
    payload = point.payload or {}
    vec = point.vector
    return {
        "track_id":  payload.get("id", str(point.id)),
        "fields":    payload.get("fields", {}),
        "embedding": vec if isinstance(vec, list) else (vec.tolist() if vec is not None else None),
        "owner":     payload.get("owner"),
        "meta":      payload.get("meta", {}),
    }


def _bake_domain(qdrant, collection, name, spec, out_dir, shard_size, scroll_batch,
                 model, dim, distance, limit=0):
    """Scroll the filtered collection into rolling gzipped NDJSON shards under a
    temp dir, then atomically swap it into place. Returns the catalog entry."""
    q_filter = _build_filter(spec.get("filter"))
    tmp_dir = out_dir + ".tmp"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    shards: list[dict] = []
    total = 0
    total_bytes = 0
    shard_idx = -1
    fh = None
    count_in_shard = 0
    # Per-domain self-description, accumulated over the baked points (free — we
    # already scroll every point). `role_sample` feeds role inference; `type_counts`
    # becomes the entityType vocabulary the client uses for its type-browse grid.
    role_sample: list[dict] = []
    type_counts: dict[str, int] = {}

    def _close_shard():
        nonlocal fh, total_bytes
        if fh is None:
            return
        fh.close()
        path = os.path.join(tmp_dir, f"shard-{shard_idx:04d}.ndjson.gz")
        size = os.path.getsize(path)
        total_bytes += size
        # Hash the FILE on disk (the gzipped bytes that get served + cached) so the
        # manifest digest is exactly what the client verifies after download.
        h = hashlib.sha256()
        with open(path, "rb") as rf:
            for chunk in iter(lambda: rf.read(1 << 20), b""):
                h.update(chunk)
        shards.append({"file": os.path.basename(path), "count": count_in_shard,
                       "bytes": size, "sha256": h.hexdigest()})

    def _open_shard():
        nonlocal fh, shard_idx, count_in_shard
        shard_idx += 1
        count_in_shard = 0
        # mtime=0 keeps the gzip header stable across re-bakes of identical input.
        fh = gzip.GzipFile(os.path.join(tmp_dir, f"shard-{shard_idx:04d}.ndjson.gz"),
                           mode="wb", mtime=0)

    offset = None
    print(f"[bake] domain {name!r}: scrolling {collection} ...")
    while True:
        points, offset = qdrant.scroll(
            collection_name=collection, scroll_filter=q_filter,
            limit=scroll_batch, offset=offset,
            with_payload=True, with_vectors=True,
        )
        for pt in points:
            row = _shard_row(pt)
            if row["embedding"] is None:
                continue
            if fh is None or count_in_shard >= shard_size:
                _close_shard()
                _open_shard()
            line = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
            fh.write(line)
            count_in_shard += 1
            total += 1
            # Accumulate self-description from the baked points only.
            et = (pt.payload or {}).get("entityType")
            if et:
                type_counts[et] = type_counts.get(et, 0) + 1
            if len(role_sample) < ROLE_SAMPLE_SIZE:
                role_sample.append(row["fields"])
            if limit and total >= limit:
                break
        if total and (total % 100000 < scroll_batch):
            print(f"[bake]   {name}: {total} points...", flush=True)
        if offset is None or (limit and total >= limit):
            break
    _close_shard()

    # Self-description so a pulled domain is renderable offline with no live
    # schema registry: inferred semantic roles (same inference the server does at
    # runtime, but over this domain's own sample) + the entityType vocabulary.
    role_map = infer_roles(role_sample)
    entity_types = [{"type": t, "count": c}
                    for t, c in sorted(type_counts.items(), key=lambda kv: -kv[1])]

    manifest = {
        "name": name,
        "description": spec.get("description", ""),
        "count": total,
        "dim": dim,
        "model": model,
        "distance": distance,
        "filter": spec.get("filter", {}),
        "created_at": int(time.time()),
        "role_map": role_map,
        "entity_types": entity_types,
        "shards": shards,
    }
    # Optional: relationship/type vocabulary from a bundle schema JSON.
    bundle = _load_bundle_schema(spec.get("bundle_schema"))
    if bundle is not None:
        manifest["bundle"] = bundle
    # Optional: presentation overlay (icons / accent colors / label & external-URL
    # overrides). Passed through verbatim — the client falls back to inferred
    # defaults when absent.
    if spec.get("presentation"):
        manifest["presentation"] = spec["presentation"]

    with open(os.path.join(tmp_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    # Atomic swap: replace any prior bake of this domain in one move.
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.replace(tmp_dir, out_dir)
    type_names = [e["type"] for e in entity_types]
    print(f"[bake] domain {name!r}: {total} points in {len(shards)} shard(s), "
          f"{total_bytes / 1e6:.1f} MB; types={type_names}; "
          f"title<-{role_map.get('title')!r} tags<-{role_map.get('tags')}")
    return {
        "name": name,
        "description": spec.get("description", ""),
        "count": total,
        "dim": dim,
        "bytes": total_bytes,
        "shard_count": len(shards),
        "entity_types": type_names,
        "manifest": f"{name}/manifest.json",
    }


def bake_main():
    args = _bake_args()
    if not os.path.exists(args.config):
        sys.exit(f"[bake] config not found: {args.config}")
    with open(args.config) as f:
        cfg = json.load(f)
    domains = cfg.get("domains", cfg)  # accept either {domains:{...}} or {...}
    if not isinstance(domains, dict) or not domains:
        sys.exit(f"[bake] no domains in {args.config}")
    if args.domain:
        if args.domain not in domains:
            sys.exit(f"[bake] domain {args.domain!r} not in config "
                     f"(have: {', '.join(domains)})")
        domains = {args.domain: domains[args.domain]}

    qdrant = make_qdrant(args)
    if not qdrant.collection_exists(args.collection):
        sys.exit(f"[bake] source collection {args.collection!r} does not exist")
    info = qdrant.get_collection(args.collection)
    vparams = info.config.params.vectors
    dim = getattr(vparams, "size", None)
    distance = str(getattr(vparams, "distance", "Cosine"))

    os.makedirs(args.cdn_dir, exist_ok=True)
    catalog_path = os.path.join(args.cdn_dir, "catalog.json")
    # Preserve catalog entries for domains we're NOT re-baking this run.
    catalog = {}
    if os.path.exists(catalog_path):
        try:
            with open(catalog_path) as f:
                for e in json.load(f).get("domains", []):
                    catalog[e["name"]] = e
        except Exception:
            pass

    for name, spec in domains.items():
        entry = _bake_domain(
            qdrant, args.collection, name, spec,
            os.path.join(args.cdn_dir, name),
            args.shard_size, args.scroll_batch,
            args.embedding_model, dim, distance, limit=args.limit,
        )
        catalog[name] = entry

    with open(catalog_path, "w") as f:
        json.dump({"generated_at": int(time.time()),
                   "embedding_model": args.embedding_model,
                   "collection": args.collection,
                   "domains": list(catalog.values())}, f, indent=2)
    print(f"[bake] catalog written: {catalog_path} ({len(catalog)} domain(s))")


# ---------------------------------------------------------------------------
# SERVE — static file CDN (FileResponse handles HTTP Range automatically)
# ---------------------------------------------------------------------------
def _serve_args():
    p = argparse.ArgumentParser(
        prog="quickbeam cdn serve",
        description="Serve baked Semantic CDN shards as static, resumable files.")
    p.add_argument("--cdn-dir", default="./cdn", help="Directory of baked shards.")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--cors", action="store_true", default=False,
                   help="Enable permissive CORS (for browser-based pulls).")
    return p.parse_args()


def build_app(cdn_dir: str, cors: bool = False):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse

    cdn_dir = os.path.abspath(cdn_dir)
    app = FastAPI(title="Fangorn Semantic CDN")

    if cors:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=["*"],
                           allow_methods=["GET"], allow_headers=["*"])

    def _safe(*parts: str) -> str:
        """Resolve a path under cdn_dir, rejecting traversal/escape."""
        path = os.path.abspath(os.path.join(cdn_dir, *parts))
        if path != cdn_dir and not path.startswith(cdn_dir + os.sep):
            raise HTTPException(status_code=400, detail="invalid path")
        return path

    @app.get("/health")
    def health():
        ok = os.path.exists(os.path.join(cdn_dir, "catalog.json"))
        return {"status": "ok" if ok else "no-catalog", "cdn_dir": cdn_dir}

    @app.get("/catalog")
    def catalog():
        path = os.path.join(cdn_dir, "catalog.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="no catalog — run `cdn bake`")
        return FileResponse(path, media_type="application/json")

    @app.get("/domains/{name}/manifest")
    def manifest(name: str):
        path = _safe(name, "manifest.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f"unknown domain {name!r}")
        return FileResponse(path, media_type="application/json")

    @app.get("/domains/{name}/shards/{file}")
    def shard(name: str, file: str):
        if not file.startswith("shard-") or not file.endswith(".ndjson.gz"):
            raise HTTPException(status_code=400, detail="invalid shard name")
        path = _safe(name, file)
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="shard not found")
        # Immutable, content-addressed bytes — cache hard. FileResponse adds an
        # ETag and honours Range requests (206) so clients resume.
        return FileResponse(
            path, media_type="application/gzip",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/")
    def root():
        return JSONResponse({"service": "fangorn-semantic-cdn",
                             "routes": ["/catalog", "/domains/{name}/manifest",
                                        "/domains/{name}/shards/{file}", "/health"]})
    return app


def serve_main():
    import uvicorn
    args = _serve_args()
    if not os.path.exists(args.cdn_dir):
        sys.exit(f"[serve] cdn dir not found: {args.cdn_dir} (run `cdn bake` first)")
    app = build_app(args.cdn_dir, cors=args.cors)
    print(f"[serve] Semantic CDN on http://{args.host}:{args.port} "
          f"(dir: {os.path.abspath(args.cdn_dir)})")
    uvicorn.run(app, host=args.host, port=args.port)
