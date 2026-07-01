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
    p.add_argument("--project", choices=["none", "umap"], default="none",
                   help="Bake a 2-D projection into each row for the Atlas view. "
                        "'umap' fits UMAP over all embeddings (needs umap-learn); "
                        "'none' lets the client project (PCA) at load time.")
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


def _project_umap(embeddings, *, n_neighbors=15, min_dist=0.1, seed=42):
    """Fit a 2-D UMAP over the document embeddings. Returns an (N, 2) list of
    [x, y] floats. Imported lazily so the dependency is only needed when a bake
    actually requests `--project umap`."""
    import numpy as np
    try:
        import umap  # umap-learn
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise SystemExit(
            "[bake] --project umap needs `umap-learn` (pip install umap-learn).") from e
    X = np.asarray(embeddings, dtype="float32")
    # n_neighbors must be < n_samples; clamp for tiny snapshots.
    nn = max(2, min(n_neighbors, len(X) - 1))
    reducer = umap.UMAP(n_components=2, n_neighbors=nn, min_dist=min_dist,
                        metric="cosine", random_state=seed)
    coords = reducer.fit_transform(X)
    return [[round(float(x), 5), round(float(y), 5)] for x, y in coords]


def _bake_domain(qdrant, collection, name, spec, out_dir, shard_size, scroll_batch,
                 model, dim, distance, limit=0, project="none"):
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
        digest = h.hexdigest()
        # TRUE content addressing: fold the digest into the filename so a re-bake of
        # changed data mints a NEW url. Shards are served `immutable, max-age=1yr`, so
        # a positional name (shard-0000) would let a browser serve last bake's bytes
        # for a year. The hashed name forces a clean cache miss instead.
        hashed_name = f"shard-{shard_idx:04d}-{digest[:12]}.ndjson.gz"
        os.replace(path, os.path.join(tmp_dir, hashed_name))
        shards.append({"file": hashed_name, "count": count_in_shard,
                       "bytes": size, "sha256": digest})

    def _open_shard():
        nonlocal fh, shard_idx, count_in_shard
        shard_idx += 1
        count_in_shard = 0
        # mtime=0 keeps the gzip header stable across re-bakes of identical input.
        fh = gzip.GzipFile(os.path.join(tmp_dir, f"shard-{shard_idx:04d}.ndjson.gz"),
                           mode="wb", mtime=0)

    # Write one row into the rolling shards (rotating files at shard_size).
    def _emit(row):
        nonlocal fh, count_in_shard, total
        if fh is None or count_in_shard >= shard_size:
            _close_shard()
            _open_shard()
        line = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
        fh.write(line)
        count_in_shard += 1
        total += 1

    # When projecting, buffer the rows so a 2-D UMAP can be fit over *all* the
    # embeddings before they are written (UMAP is a global operation). Otherwise
    # stream straight to disk as before (no buffering, low memory).
    projecting = project == "umap"
    buffered: list[dict] = []
    scrolled = 0

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
            # Accumulate self-description from the baked points only.
            et = (pt.payload or {}).get("entityType")
            if et:
                type_counts[et] = type_counts.get(et, 0) + 1
            if len(role_sample) < ROLE_SAMPLE_SIZE:
                role_sample.append(row["fields"])
            if projecting:
                buffered.append(row)
            else:
                _emit(row)
            scrolled += 1
            if limit and scrolled >= limit:
                break
        if scrolled and (scrolled % 100000 < scroll_batch):
            print(f"[bake]   {name}: {scrolled} points...", flush=True)
        if offset is None or (limit and scrolled >= limit):
            break

    if projecting:
        print(f"[bake]   {name}: fitting UMAP over {len(buffered)} embeddings...", flush=True)
        coords = _project_umap([r["embedding"] for r in buffered])
        for row, xy in zip(buffered, coords):
            row["proj"] = xy
        for row in buffered:
            _emit(row)
    _close_shard()

    # Self-description so a pulled domain is renderable offline with no live
    # schema registry: inferred semantic roles (same inference the server does at
    # runtime, but over this domain's own sample) + the entityType vocabulary.
    role_map = infer_roles(role_sample)
    # Role inference is heuristic and mis-assigns rich schemas (e.g. picking a
    # review/byline field as `subtitle` or folding `reviews`/`businesses` into
    # `tags`). Let the domain spec pin/override individual keys for a clean,
    # deterministic client render. Shallow-merged so unset keys still infer.
    spec_roles = spec.get("role_map")
    if isinstance(spec_roles, dict):
        role_map = {**role_map, **spec_roles}
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
    # Record whether a 2-D projection was baked into the rows (the Atlas view reads
    # row.proj when present; otherwise it projects client-side).
    if project and project != "none":
        manifest["projection"] = project
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
            project=args.project,
        )
        catalog[name] = entry

    with open(catalog_path, "w") as f:
        json.dump({"generated_at": int(time.time()),
                   "embedding_model": args.embedding_model,
                   "collection": args.collection,
                   "domains": list(catalog.values())}, f, indent=2)
    print(f"[bake] catalog written: {catalog_path} ({len(catalog)} domain(s))")


# ---------------------------------------------------------------------------
# APPEND — deliver newly-embedded points as a delta shard (no full re-bake)
# ---------------------------------------------------------------------------
# `cdn bake` re-scrolls the WHOLE collection and rewrites the domain's shard, so a
# single new record mints a fresh content-hash shard containing everything — the
# client re-downloads the whole snapshot. `cdn append` instead writes ONLY the
# points not already in a shard as one additional content-addressed shard and
# appends it to the (mutable, no-cache) manifest. Existing shards are immutable and
# untouched, so a returning client pulls only the delta (every old shard is a hard
# HTTP cache hit). This is the incremental-delivery path the live pipeline uses.
def _append_args():
    p = argparse.ArgumentParser(
        prog="quickbeam cdn append",
        description="Append newly-embedded points to an already-baked domain as a "
                    "delta shard, without rewriting existing shards.")
    p.add_argument("--config", default="domains.json",
                   help="Domain config — the domain's `filter` selects which points "
                        "to consider (same population `cdn bake` used).")
    p.add_argument("--cdn-dir", default="./cdn", help="Baked CDN directory.")
    p.add_argument("--collection", default="fangorn", help="Source Qdrant collection.")
    p.add_argument("--domain", required=True,
                   help="Domain to append to (must already be baked).")
    p.add_argument("--entity-type", action="append", default=[], dest="entity_types",
                   metavar="TYPE",
                   help="Narrow the scan to these entityTypes (repeatable). "
                        "Default: the domain's configured filter.")
    p.add_argument("--owner", action="append", default=[], dest="owners",
                   metavar="ADDRESS",
                   help="Narrow the scan to these owners (repeatable).")
    p.add_argument("--scroll-batch", type=int, default=2000,
                   help="Qdrant scroll page size.")
    _add_qdrant_args(p)
    return p.parse_args()


def _sha256_path(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _existing_baked_ids(domain_dir: str, manifest: dict) -> set:
    """Read every existing shard once to collect the track_ids already delivered.
    Append is idempotent: a point already in a shard is never re-emitted, so
    re-running append (after a partial run, or after re-embedding) is safe."""
    ids: set = set()
    for s in manifest.get("shards", []):
        path = os.path.join(domain_dir, s["file"])
        if not os.path.exists(path):
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line)["track_id"])
                except Exception:  # noqa: BLE001 - skip a corrupt line, keep going
                    pass
    return ids


def _sync_catalog(cdn_dir: str, domain: str, manifest: dict) -> None:
    """Keep catalog.json's domain entry in sync after an append (count / bytes /
    shard_count / entity_types). Bytes is re-summed from the manifest's shards."""
    catalog_path = os.path.join(cdn_dir, "catalog.json")
    if not os.path.exists(catalog_path):
        return
    with open(catalog_path) as f:
        cat = json.load(f)
    total_bytes = sum(s.get("bytes", 0) for s in manifest.get("shards", []))
    for e in cat.get("domains", []):
        if e.get("name") == domain:
            e["count"] = manifest.get("count", e.get("count"))
            e["bytes"] = total_bytes
            e["shard_count"] = len(manifest.get("shards", []))
            e["entity_types"] = [t["type"] for t in manifest.get("entity_types", [])]
    cat["generated_at"] = int(time.time())
    with open(catalog_path, "w") as f:
        json.dump(cat, f, indent=2)


def append_domain(qdrant, collection: str, cdn_dir: str, domain: str,
                  config_path: str | None = None,
                  entity_types: list | None = None, owners: list | None = None,
                  scroll_batch: int = 2000) -> dict | None:
    """Scroll `collection` for points not already in `domain`'s shards and, if any,
    write them as a delta shard. Returns the new shard entry (or None if nothing new
    / the domain isn't baked). Reusable by the CLI and the live watcher — the watcher
    passes its already-open Qdrant client so a delta ships right after each embed
    cycle, closing the on-chain → embed → deliver loop with no full re-bake."""
    domain_dir = os.path.join(cdn_dir, domain)
    manifest_path = os.path.join(domain_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"[append] domain {domain!r} not baked yet (no {manifest_path}) "
              f"— run `cdn bake` first; skipping")
        return None
    with open(manifest_path) as f:
        manifest = json.load(f)

    # Resolve the scan filter: explicit entity_types/owners override; otherwise reuse
    # the domain's configured filter so the append selects the same population as bake.
    spec_filter: dict = {}
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            cfg = json.load(f).get("domains", {})
        spec_filter = (cfg.get(domain) or {}).get("filter", {}) or {}
    scan = dict(spec_filter)
    if entity_types:
        scan["entityType"] = entity_types
    if owners:
        scan["owner"] = owners
    q_filter = _build_filter(scan)

    baked = _existing_baked_ids(domain_dir, manifest)
    print(f"[append] domain {domain!r}: {len(baked)} points already delivered")

    # Scroll the filtered collection; keep only points not already in a shard.
    new_rows: list[dict] = []
    type_counts: dict[str, int] = {}
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=collection, scroll_filter=q_filter,
            limit=scroll_batch, offset=offset,
            with_payload=True, with_vectors=True,
        )
        for pt in points:
            row = _shard_row(pt)
            if row["embedding"] is None or row["track_id"] in baked:
                continue
            new_rows.append(row)
            et = (pt.payload or {}).get("entityType")
            if et:
                type_counts[et] = type_counts.get(et, 0) + 1
        if offset is None:
            break

    if not new_rows:
        print("[append] no new points — manifest unchanged")
        return None

    entry = write_delta_shard(cdn_dir, domain, new_rows, type_counts,
                              manifest=manifest, manifest_path=manifest_path)
    print(f"[append] domain {domain!r}: +{len(new_rows)} points in {entry['file']} "
          f"({entry['bytes'] / 1e3:.1f} KB); types={dict(type_counts)}; "
          f"total now {manifest['count']}")
    return entry


def append_main():
    args = _append_args()
    qdrant = make_qdrant(args)
    if not qdrant.collection_exists(args.collection):
        sys.exit(f"[append] source collection {args.collection!r} does not exist")
    append_domain(qdrant, args.collection, args.cdn_dir, args.domain,
                  config_path=args.config, entity_types=args.entity_types,
                  owners=args.owners, scroll_batch=args.scroll_batch)


def write_delta_shard(cdn_dir: str, domain: str, new_rows: list, type_counts: dict,
                      manifest: dict | None = None,
                      manifest_path: str | None = None) -> dict:
    """Write `new_rows` as ONE new content-addressed delta shard and fold it into the
    domain's mutable manifest + catalog. Existing shards are never touched, so a
    client pulls only this file. Returns the shard entry. Reusable by both `cdn
    append` (which scrolls Qdrant) and the watcher (which already holds the rows).

    `manifest`/`manifest_path` are loaded from disk when not supplied. `manifest` is
    mutated in place so a caller that passed one sees the bumped counts."""
    domain_dir = os.path.join(cdn_dir, domain)
    if manifest_path is None:
        manifest_path = os.path.join(domain_dir, "manifest.json")
    if manifest is None:
        with open(manifest_path) as f:
            manifest = json.load(f)

    # The shard index continues the existing sequence; the digest is folded into the
    # filename so the URL is immutable (a re-bake/append mints a fresh URL, never
    # serving stale bytes from a year-long cache).
    shard_idx = len(manifest.get("shards", []))
    tmp_path = os.path.join(domain_dir, f"shard-{shard_idx:04d}.ndjson.gz")
    with gzip.GzipFile(tmp_path, mode="wb", mtime=0) as fh:  # mtime=0 → stable header
        for row in new_rows:
            fh.write((json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
    size = os.path.getsize(tmp_path)
    digest = _sha256_path(tmp_path)
    hashed_name = f"shard-{shard_idx:04d}-{digest[:12]}.ndjson.gz"
    os.replace(tmp_path, os.path.join(domain_dir, hashed_name))
    entry = {"file": hashed_name, "count": len(new_rows), "bytes": size, "sha256": digest}

    # Update the mutable, no-cache manifest: append the shard, bump the count, merge
    # the entityType vocabulary. role_map is deliberately left stable across appends
    # (re-inferring per delta would let the client's field mapping drift).
    manifest.setdefault("shards", []).append(entry)
    manifest["count"] = manifest.get("count", 0) + len(new_rows)
    manifest["created_at"] = int(time.time())
    et_map = {e["type"]: e["count"] for e in manifest.get("entity_types", [])}
    for t, c in type_counts.items():
        et_map[t] = et_map.get(t, 0) + c
    manifest["entity_types"] = [{"type": t, "count": c}
                                for t, c in sorted(et_map.items(), key=lambda kv: -kv[1])]
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    _sync_catalog(cdn_dir, domain, manifest)
    return entry


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

    # The catalog + manifests are the MUTABLE pointers to the immutable shards: a
    # re-bake rewrites them to reference new content-hashed shard files. They must
    # always be revalidated (cheap 304s via FileResponse's ETag) or a client would
    # keep pulling a stale manifest and never learn the new shard names.
    _NO_CACHE = {"Cache-Control": "no-cache"}

    @app.get("/catalog")
    def catalog():
        path = os.path.join(cdn_dir, "catalog.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="no catalog — run `cdn bake`")
        return FileResponse(path, media_type="application/json", headers=_NO_CACHE)

    @app.get("/domains/{name}/manifest")
    def manifest(name: str):
        path = _safe(name, "manifest.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail=f"unknown domain {name!r}")
        return FileResponse(path, media_type="application/json", headers=_NO_CACHE)

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
