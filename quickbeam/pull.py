"""
pull.py — the Semantic CDN client. `quickbeam pull <domain>`

Downloads a domain's immutable shards from a Semantic CDN (cdn.py), verifies them
by sha256, and loads them into a LOCAL Qdrant collection. After a pull, the user
queries entirely offline with `quickbeam serve --collection <domain>` — the CDN
never sees a query. This is the user-facing half of "knowledge is public, intent
is private."

Downloads are resumable (HTTP Range continues a partial `.part`) and idempotent
(deterministic point ids), so an interrupted pull is safe to re-run.
"""
import argparse
import gzip
import hashlib
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from qdrant_client import QdrantClient
from qdrant_client import models as qmodels


def _str_to_uuid(s: str) -> str:
    """Deterministic point id — matches server.py / embeddings.py so a re-pull
    overwrites the same points instead of duplicating them."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, s))


def _args():
    p = argparse.ArgumentParser(
        prog="quickbeam pull",
        description="Pull a domain from a Semantic CDN into a local Qdrant collection.")
    p.add_argument("domain", help="Domain name to pull (see the CDN's /catalog).")
    p.add_argument("--cdn-url", default="http://localhost:8090",
                   help="Base URL of the Semantic CDN.")
    p.add_argument("--collection", default=None,
                   help="Local Qdrant collection to load into (default: the domain name).")
    p.add_argument("--cache-dir", default="./db/cdn_cache",
                   help="Where downloaded shards are cached.")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel shard downloads.")
    p.add_argument("--batch", type=int, default=500, help="Upsert batch size.")
    p.add_argument("--reset", action="store_true", default=False,
                   help="Recreate the local collection before loading.")
    p.add_argument("--download-only", action="store_true", default=False,
                   help="Fetch + verify shards but don't load into Qdrant.")
    # Local Qdrant target.
    p.add_argument("--qdrant-url", default=None, metavar="URL")
    p.add_argument("--qdrant-api-key", default=None)
    p.add_argument("--qdrant-host", default="localhost")
    p.add_argument("--qdrant-port", type=int, default=6333)
    p.add_argument("--qdrant-grpc-port", type=int, default=6334)
    return p.parse_args()


def _make_qdrant(args) -> QdrantClient:
    if args.qdrant_url:
        return QdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key,
                            prefer_grpc=True, timeout=120)
    return QdrantClient(host=args.qdrant_host, port=args.qdrant_port,
                        grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=120)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_shard(base: str, domain: str, shard: dict, dest_dir: str,
                    retries: int = 5) -> str:
    """Resumable download of one shard, verified against its manifest sha256.
    Returns the local path. Skips the network entirely if a verified copy exists."""
    fname = shard["file"]
    final = os.path.join(dest_dir, fname)
    part = final + ".part"
    expected = shard.get("sha256")
    expected_size = shard.get("bytes")

    if os.path.exists(final):
        if expected is None or _sha256_file(final) == expected:
            return final
        os.remove(final)  # corrupt cache — refetch

    url = f"{base}/domains/{domain}/shards/{fname}"
    last_err = None
    for attempt in range(retries):
        try:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if expected_size and have > expected_size:  # overshoot — restart
                os.remove(part); have = 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                # 206 = resumed; 200 = full (server ignored Range -> start fresh).
                if have and r.status_code == 200:
                    have = 0
                    mode = "wb"
                elif r.status_code in (200, 206):
                    mode = "ab" if have else "wb"
                else:
                    r.raise_for_status()
                    mode = "wb"
                with open(part, mode) as f:
                    for chunk in r.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
            got = _sha256_file(part)
            if expected and got != expected:
                os.remove(part)
                raise ValueError(f"sha256 mismatch for {fname} "
                                 f"(got {got[:12]}, want {expected[:12]})")
            os.replace(part, final)
            return final
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[pull]   {fname}: {e} (attempt {attempt + 1}/{retries})")
    raise RuntimeError(f"failed to download {fname}: {last_err}")


def _ensure_collection(qdrant, name, dim, distance, reset):
    dist = qmodels.Distance.COSINE
    if isinstance(distance, str) and distance.lower().startswith("dot"):
        dist = qmodels.Distance.DOT
    elif isinstance(distance, str) and distance.lower().startswith("eucl"):
        dist = qmodels.Distance.EUCLID
    if reset and qdrant.collection_exists(name):
        print(f"[pull] resetting local collection {name!r}")
        qdrant.delete_collection(name)
    if not qdrant.collection_exists(name):
        qdrant.create_collection(
            name, vectors_config=qmodels.VectorParams(size=dim, distance=dist))
        print(f"[pull] created local collection {name!r} dim={dim} distance={dist}")


def _load_shard(qdrant, collection, path, batch):
    """Stream a gzipped NDJSON shard into Qdrant. Mirrors /bundle/import."""
    buf: list[qmodels.PointStruct] = []
    loaded = 0

    def flush():
        nonlocal loaded
        if buf:
            qdrant.upsert(collection_name=collection, points=buf, wait=True)
            loaded += len(buf)
            buf.clear()

    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            tid = row.get("track_id")
            vec = row.get("embedding")
            if not tid or not vec:
                continue
            buf.append(qmodels.PointStruct(
                id=_str_to_uuid(tid),
                vector=vec,
                payload={
                    "id":         tid,
                    "entityType": (row.get("fields") or {}).get("entityType"),
                    "owner":      row.get("owner"),
                    "fields":     row.get("fields", {}),
                    "meta":       row.get("meta", {}),
                },
            ))
            if len(buf) >= batch:
                flush()
    flush()
    return loaded


def main():
    args = _args()
    base = args.cdn_url.rstrip("/")
    collection = args.collection or args.domain

    # Fetch the domain manifest.
    murl = f"{base}/domains/{args.domain}/manifest"
    print(f"[pull] fetching manifest: {murl}")
    try:
        manifest = requests.get(murl, timeout=30).json()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[pull] could not fetch manifest: {e}")
    if not manifest.get("shards"):
        sys.exit(f"[pull] domain {args.domain!r} has no shards")
    shards = manifest["shards"]
    total = manifest.get("count", 0)
    dim = manifest.get("dim")
    print(f"[pull] domain {args.domain!r}: {total} points, {len(shards)} shard(s), "
          f"dim={dim}, model={manifest.get('model')}")

    dest_dir = os.path.join(args.cache_dir, args.domain)
    os.makedirs(dest_dir, exist_ok=True)

    # Download + verify shards in parallel.
    paths: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(_download_shard, base, args.domain, s, dest_dir): s["file"]
                for s in shards}
        done = 0
        for fut in as_completed(futs):
            fname = futs[fut]
            paths[fname] = fut.result()
            done += 1
            print(f"[pull] shard {done}/{len(shards)} ready: {fname}")

    if args.download_only:
        print(f"[pull] download-only — {len(paths)} verified shard(s) in {dest_dir}")
        return

    # Load into local Qdrant, in manifest order (deterministic).
    qdrant = _make_qdrant(args)
    if not dim:
        sys.exit("[pull] manifest has no dim — cannot size the collection")
    _ensure_collection(qdrant, collection, dim, manifest.get("distance", "Cosine"),
                       args.reset)

    loaded = 0
    for s in shards:
        loaded += _load_shard(qdrant, collection, paths[s["file"]], args.batch)
        print(f"[pull]   loaded {loaded}/{total}", flush=True)

    final_count = qdrant.count(collection).count
    print(f"[pull] done — {loaded} points loaded into {collection!r} "
          f"(collection now holds {final_count}).")
    print(f"[pull] query it locally:  quickbeam serve --collection {collection}")


if __name__ == "__main__":
    main()
