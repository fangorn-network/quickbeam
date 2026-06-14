import certifi
import os
import io
import sys
import argparse
import asyncio
import aiohttp
import hashlib
import json
import uuid

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client import models
from fastembed import TextEmbedding
from quickbeam.roles import infer_roles
from tqdm import tqdm

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

os.environ['SSL_CERT_FILE'] = certifi.where()

# ---------------------------------------------------------------------------
# CONFIG & ARGS
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Fangorn Qdrant Ingestion CLI Builder")
    parser.add_argument("--schema", "-s", action="append", dest="schemas", default=[])
    parser.add_argument("--primary", "-p", default=None)
    parser.add_argument("--bundle", default=None,
                        help="Bundle schema as name=schemaId. Uses edge-walk join instead of --primary track-id join.")
    parser.add_argument("--root-type", default="Track",
                        help="Bundle root node type — one record is emitted per root node.")
    parser.add_argument("--subgraph-url", default="https://gateway.thegraph.com/api/subgraphs/id/8SgbhtiitpAhEfyTgeAHxHH5DQ2gTygUuXgc3b7MCFyc")
    parser.add_argument("--graph-api-key", default="")
    parser.add_argument("--ipfs-gateway", default="https://gateway.pinata.cloud/ipfs")
    parser.add_argument("--ipfs-gateway-key", default=None)
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--qdrant-grpc-port", type=int, default=6334)
    parser.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    parser.add_argument("--collection", default="fangorn")
    parser.add_argument("--searchable-fields", default="auto")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--ipfs-timeout", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--reset", action="store_true", default=False)
    parser.add_argument("--embedding-model", default="nomic-ai/nomic-embed-text-v1.5")
    parser.add_argument("--dim", type=int, default=256, help="Matryoshka output dim: 256, 512, or 768")
    parser.add_argument("--embed-batch", type=int, default=16)
    parser.add_argument("--role-map-file", default="./db/role_map.json")
    parser.add_argument("--umap", action="store_true", default=False)
    parser.add_argument("--umap-only", action="store_true", default=False)
    parser.add_argument("--umap-neighbors", type=int, default=15)
    parser.add_argument("--umap-min-dist", type=float, default=0.05)
    return parser.parse_args()

MODEL_DIM_MAP = {
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384
}

# ---------------------------------------------------------------------------
# SHARED HELPERS
# ---------------------------------------------------------------------------
def _load_checkpoint(path):
    try:
        with open(path) as f:
            ck = json.load(f)
            # Legacy format: top-level keys were manifest cids
            if "manifests" not in ck:
                ck = {"manifests": ck, "processed_track_ids": []}
            ck.setdefault("completed_manifest_cids", [])
            ck.setdefault("processed_track_ids", [])
            return ck
    except Exception:
        return {"manifests": {}, "processed_track_ids": [], "completed_manifest_cids": []}


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


def _init_embed_engine(args):
    """Init TextEmbedding, first clearing any corrupted model cache snapshots."""
    import glob, shutil, tempfile
    cache_root = os.environ.get("FASTEMBED_CACHE_PATH", os.path.join(tempfile.gettempdir(), "fastembed_cache"))
    slug = args.embedding_model.replace("/", "--")
    for snap in glob.glob(os.path.join(cache_root, f"models--{slug}", "snapshots", "*")):
        if os.path.isdir(snap) and not os.path.isfile(os.path.join(snap, "onnx", "model.onnx")):
            print(f"[Builder] Corrupt model cache at {snap!r}, removing for re-download...")
            shutil.rmtree(snap)
    return TextEmbedding(
        model_name=args.embedding_model,
        max_length=256,
        providers=[
            ("CUDAExecutionProvider", {
                "device_id": 0,
                "arena_extend_strategy": "kNextPowerOfTwo",
                "gpu_mem_limit": 3 * 1024 * 1024 * 1024,
                "cudnn_conv_algo_search": "DEFAULT",
            }),
            "CPUExecutionProvider",
        ]
    )

# ---------------------------------------------------------------------------
# MATRYOSHKA TRUNCATION
# ---------------------------------------------------------------------------
def matryoshka(vec, dim):
    x = np.asarray(vec, dtype=np.float32)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-5)
    x = x[:dim]
    n = np.linalg.norm(x)
    return (x / n).astype(np.float32).tolist() if n else x.tolist()

# ---------------------------------------------------------------------------
# PAYLOAD INDEXES
# ---------------------------------------------------------------------------
def ensure_indexes(qdrant, collection):
    specs = [
        ("fields.title",    models.TextIndexParams(type="text", tokenizer=models.TokenizerType.WORD, lowercase=True)),
        ("fields.byArtist", models.TextIndexParams(type="text", tokenizer=models.TokenizerType.WORD, lowercase=True)),
        ("owner",           models.KeywordIndexParams(type="keyword")),
    ]
    for field, schema in specs:
        try:
            qdrant.create_payload_index(collection_name=collection, field_name=field, field_schema=schema)
            print(f"[index] created {field}")
        except Exception as e:
            print(f"[index] {field} already present ({type(e).__name__})")

# ---------------------------------------------------------------------------
# UMAP PRECOMPUTE
# ---------------------------------------------------------------------------
def write_umap_coords(qdrant, collection, neighbors, min_dist):
    try:
        import umap
    except ImportError:
        print("[umap] umap-learn not installed. Run: pip install umap-learn")
        return
    import numpy as np
    from tqdm import tqdm
    import gc
    import tempfile, os

    total = qdrant.count(collection).count
    print(f"[umap] pulling {total} vectors ...")

    probe, _ = qdrant.scroll(collection, limit=1, with_vectors=True, with_payload=False)
    if not probe:
        print("[umap] collection empty, nothing to project")
        return
    dims = len(probe[0].vector)

    arr_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mmap")
    arr_tmp.close()
    arr = np.memmap(arr_tmp.name, dtype=np.float32, mode="w+", shape=(total, dims))

    ids = []
    cursor = 0
    offset = None
    pbar = tqdm(total=total, desc="  ↳ pulling vectors", unit=" vec")
    while True:
        pts, offset = qdrant.scroll(
            collection, limit=500, offset=offset,
            with_vectors=True, with_payload=False
        )
        if not pts:
            break
        for p in pts:
            ids.append(p.id)
            arr[cursor] = p.vector
            cursor += 1
        pbar.update(len(pts))
        if offset is None:
            break
    pbar.close()

    n = cursor

    sample_size = min(30_000, n)
    print(f"[umap] fitting UMAP on {sample_size}-vector sample ...")
    indices = np.random.choice(n, sample_size, replace=False)
    train_sample = np.array(arr[indices], dtype=np.float32)

    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=min(neighbors, sample_size - 1),
        min_dist=min_dist,
        metric="cosine",
        low_memory=True,
        random_state=42,
        verbose=True,
    )
    reducer.fit(train_sample)
    del train_sample
    gc.collect()

    proj_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mmap")
    proj_tmp.close()
    proj = np.memmap(proj_tmp.name, dtype=np.float32, mode="w+", shape=(n, 2))

    transform_batch_size = 10_000
    for i in tqdm(range(0, n, transform_batch_size), desc="  ↳ transforming"):
        end = min(i + transform_batch_size, n)
        chunk = np.array(arr[i:end], dtype=np.float32)
        proj[i:end] = reducer.transform(chunk)
        del chunk
        gc.collect()

    del arr
    gc.collect()
    os.unlink(arr_tmp.name)

    for ax in range(2):
        mn, mx = float(proj[:, ax].min()), float(proj[:, ax].max())
        rng = (mx - mn) or 1.0
        proj[:, ax] = (proj[:, ax] - mn) / rng * 2 - 1

    print("[umap] writing px/py back to payloads ...")
    B = 1000
    bad = 0
    for i in tqdm(range(0, n, B), desc="  ↳ set_payload", unit=" batch"):
        ops = []
        batch_proj = np.array(proj[i:i + B])
        for pid, row in zip(ids[i:i + B], batch_proj):
            x, y = float(row[0]), float(row[1])
            if not (np.isfinite(x) and np.isfinite(y)):
                bad += 1
                x, y = 0.0, 0.0
            ops.append(models.SetPayloadOperation(
                set_payload=models.SetPayload(
                    payload={"px": x, "py": y},
                    points=[pid],
                )
            ))
        qdrant.batch_update_points(collection_name=collection, update_operations=ops)

    if bad:
        print(f"[umap] warning: {bad} points had non-finite coords, zeroed out")

    del proj
    os.unlink(proj_tmp.name)
    print(f"[umap] done — px/py on {n} points. Snapshot now to bake it in.")

# ---------------------------------------------------------------------------
# SUBGRAPH QUERIES
# block_gt variants add `blockNumber_gt` to the where clause for incremental
# polling in the watcher (avoids re-scanning the full event history each cycle).
# ---------------------------------------------------------------------------
_PUBLISHES_Q = "query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!) { manifestPublisheds(where: { schemaId: $schemaId }, first: $first, skip: $skip, orderBy: blockNumber, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
_PUBLISHES_Q_FROM = "query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!, $blockGt: BigInt!) { manifestPublisheds(where: { schemaId: $schemaId, blockNumber_gt: $blockGt }, first: $first, skip: $skip, orderBy: blockNumber, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
_UPDATES_Q = "query Updates($schemaId: Bytes!, $first: Int!, $skip: Int!) { manifestUpdateds(where: { schemaId: $schemaId }, first: $first, skip: $skip, orderBy: version, orderDirection: desc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"
_UPDATES_Q_FROM = "query Updates($schemaId: Bytes!, $first: Int!, $skip: Int!, $blockGt: BigInt!) { manifestUpdateds(where: { schemaId: $schemaId, blockNumber_gt: $blockGt }, first: $first, skip: $skip, orderBy: version, orderDirection: desc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"

async def _query_subgraph_async(url, api_key, query, variables):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with aiohttp.ClientSession() as session:
        for attempt in range(5):
            try:
                async with session.post(url, json={"query": query, "variables": variables}, headers=headers, timeout=30) as resp:
                    if resp.status in {429, 500, 502, 503, 504}: raise Exception()
                    resp.raise_for_status()
                    data = await resp.json()
                    if "errors" in data: raise RuntimeError(data["errors"])
                    return data["data"]
            except Exception:
                if attempt == 4: raise
                await asyncio.sleep(1 + attempt)


async def _fetch_all_events_async(url, api_key, schema_id, page_size, block_gt=None):
    publishes, updates = [], []
    pairs = [
        (publishes, _PUBLISHES_Q_FROM if block_gt is not None else _PUBLISHES_Q, "manifestPublisheds"),
        (updates,   _UPDATES_Q_FROM   if block_gt is not None else _UPDATES_Q,   "manifestUpdateds"),
    ]
    for target, query, key in pairs:
        skip = 0
        pbar = tqdm(desc=f"  ↳ Fetching {key}", unit=" events", leave=False)
        while True:
            variables = {"schemaId": schema_id, "first": page_size, "skip": skip}
            if block_gt is not None:
                variables["blockGt"] = block_gt
            data = await _query_subgraph_async(url, api_key, query, variables)
            batch = data.get(key, [])
            target.extend(batch)
            pbar.update(len(batch))
            if len(batch) < page_size:
                break
            skip += page_size
        pbar.close()
    return publishes, updates

# ---------------------------------------------------------------------------
# IPFS HELPERS  
# ---------------------------------------------------------------------------
_B58_ALPHABET = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

def _b58encode(v: bytes) -> str:
    leading = len(v) - len(v.lstrip(b'\x00'))
    n = int.from_bytes(v, 'big')
    res = []
    while n:
        n, r = divmod(n, 58)
        res.append(_B58_ALPHABET[r])
    return ('1' * leading) + bytes(reversed(res)).decode('ascii')

def _cid_to_path(cid: str) -> str:
    if cid.startswith(('0x', '0X')):
        raw = bytes.fromhex(cid[2:])
        if len(raw) == 34 and raw[0] == 0x12 and raw[1] == 0x20:
            return _b58encode(raw)
    return cid

async def _fetch_json(session, sem, url, timeout, pbar):
    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    res = json.loads(await resp.text())
                    pbar.update(1)
                    return res
                print(f"\n[IPFS] {resp.status} {url}", flush=True)
        except Exception as e:
            print(f"\n[IPFS] error fetching {url}: {e}", flush=True)
        pbar.update(1)
        return None

async def fetch_all_ipfs(cids, gateway, timeout, concurrency, desc="Downloading IPFS", headers=None):
    if not cids: return {}
    sem = asyncio.Semaphore(concurrency)
    pbar = tqdm(total=len(cids), desc=f"  ↳ {desc}", unit=" file")
    async with aiohttp.ClientSession(headers=headers or {}) as session:
        tasks = [
            asyncio.create_task(_fetch_json(session, sem, f"{gateway.rstrip('/')}/{_cid_to_path(cid)}", timeout, pbar))
            for cid in cids
        ]
        results = await asyncio.gather(*tasks)
    pbar.close()
    return dict(zip(cids, results))

def _track_id(fields: dict, prefer: str | None = None) -> str:
    if prefer:
        return str(prefer).strip().removeprefix("track:")
    for key in ["trackId", "track_id", "id", "contentId"]:
        if fields.get(key): return str(fields[key]).strip().removeprefix("track:")
    artist = str(fields.get("artist") or "").strip()
    title  = str(fields.get("title")  or "").strip()
    if artist and title: return hashlib.sha256(f"{artist}:{title}".encode()).hexdigest()[:24]
    return str(uuid.uuid4())[:12]

# ---------------------------------------------------------------------------
# BUNDLE JOIN — async generator, one manifest at a time
#
# Yields (manifest_cid, [records]) for each pending bundle manifest.
# Processing one manifest at a time keeps memory bounded regardless of
# collection size; chunk data is freed before the next manifest is fetched.
#
# Parameters
#   completed_manifest_cids : set of already-finished manifest CIDs to skip
#   owner_filter            : set of lowercase owner addresses (None = any)
#   name_filter             : set of lowercase dataset names  (None = any)
#   block_gt                : if set, only query events with blockNumber > this
# ---------------------------------------------------------------------------
async def build_bundle_joined_data(
    args,
    schema_id,
    root_type,
    completed_manifest_cids=None,
    owner_filter=None,
    name_filter=None,
    block_gt=None,
):
    completed = completed_manifest_cids or set()

    print(f"\n[Builder] Querying Subgraph for bundle ManifestPublished events...")
    publishes, updates = await _fetch_all_events_async(
        args.subgraph_url, args.graph_api_key, schema_id, args.page_size,
        block_gt=block_gt
    )

    cids_meta = {}
    for p in publishes: cids_meta[p["manifestCid"]] = p
    for u in updates:   cids_meta[u["manifestCid"]] = u

    if not cids_meta:
        return

    # Apply filter hierarchy: owner → dataset name
    if owner_filter:
        cids_meta = {c: m for c, m in cids_meta.items()
                     if m.get("owner", "").lower() in owner_filter}
    if name_filter:
        cids_meta = {c: m for c, m in cids_meta.items()
                     if m.get("name", "").lower() in name_filter}

    pending_cids = [c for c in cids_meta if c not in completed]
    if not pending_cids:
        print("[Builder] No pending manifests after filters.")
        return

    print(f"[Builder] {len(pending_cids)} pending manifests (skipped {len(cids_meta) - len(pending_cids)} completed).")

    gw_headers = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}

    # Fetch only the manifest envelopes (small JSON), not chunks yet.
    manifests = await fetch_all_ipfs(
        pending_cids, args.ipfs_gateway, args.ipfs_timeout,
        args.concurrency, desc="Bundle Manifests", headers=gw_headers
    )

    for mcid in pending_cids:
        m = manifests.get(mcid)
        if not m or m.get("version") != 3:
            print(f"[Builder] Skipping invalid manifest {_cid_to_path(mcid)!r}: {str(m)[:120]!r}")
            continue

        node_cids = [c["dataCid"] for c in m.get("nodeChunks", [])]
        edge_cid  = (m.get("edgeChunk") or {}).get("dataCid")
        if edge_cid is None:
            print(f"[Builder] Skipping manifest {_cid_to_path(mcid)!r} — missing edgeChunk")
            continue

        # Fetch only this manifest's chunks, then free them before moving on.
        # This bounds RAM to one manifest's data at a time.
        print(f"[Builder] Fetching {len(node_cids) + 1} chunks for {_cid_to_path(mcid)[:16]}...")
        chunks = await fetch_all_ipfs(
            node_cids + [edge_cid], args.ipfs_gateway, args.ipfs_timeout,
            args.concurrency, desc="  Chunks", headers=gw_headers
        )

        meta = cids_meta[mcid]
        nodes_by_id: dict = {}
        for ncid in node_cids:
            for node in (chunks.get(ncid) or []):
                nodes_by_id[node["id"]] = node
        edges = chunks.get(edge_cid) or []

        out: dict = {}
        for e in edges:
            out.setdefault((e["from"], e["rel"]), []).append(e["to"])

        records = []
        for node in nodes_by_id.values():
            if node.get("type") != root_type:
                continue
            fields = dict(node.get("fields", {}))
            for (frm, _rel), tos in out.items():
                if frm != node["id"]:
                    continue
                for tid in tos:
                    nb = nodes_by_id.get(tid)
                    if nb:
                        fields.update(nb.get("fields", {}))
            records.append({
                "track_id": _track_id(fields, prefer=node.get("id")),
                "fields":   fields,
                "meta":     meta,
            })

        del chunks, nodes_by_id, edges, out
        import gc; gc.collect()

        if records:
            yield mcid, records
        else:
            print(f"[Builder] Manifest {_cid_to_path(mcid)!r} had no {root_type!r} nodes — skipping.")

# ---------------------------------------------------------------------------
# EMBED + UPLOAD  (shared by build and watch)
#
# Embeds records in SAVE_BATCH_SIZE chunks and uploads to Qdrant.
# Writes partial progress to checkpoint after each batch for crash recovery.
# ---------------------------------------------------------------------------
async def _embed_and_upload(args, qdrant, embed_engine, records, role_map, dim, truncate, checkpoint):
    SAVE_BATCH_SIZE = 5000
    n_batches = max(1, (len(records) + SAVE_BATCH_SIZE - 1) // SAVE_BATCH_SIZE)
    for i in range(0, len(records), SAVE_BATCH_SIZE):
        chunk = records[i: i + SAVE_BATCH_SIZE]
        if n_batches > 1:
            print(f"  [Embed] sub-batch {i // SAVE_BATCH_SIZE + 1}/{n_batches} ({len(chunk)} records)")

        texts = []
        for item in chunk:
            fields = item["fields"]
            if args.searchable_fields == "auto":
                tags = " ".join(
                    fields.get(t, "") if isinstance(fields.get(t), str) else ""
                    for t in role_map.get("tags", [])
                )
                text_str = f"Title: {fields.get(role_map.get('title', ''), '')}. Tags: {tags}"
            else:
                text_str = " ".join(str(fields[k]) for k in args.searchable_fields.split(",") if fields.get(k))
            texts.append(f"search_document: {text_str[:1000]}")

        vectors = []
        SUB_CHUNK_SIZE = 1000
        with tqdm(total=len(texts), desc="  ↳ Embedding", unit=" doc") as pbar:
            for si in range(0, len(texts), SUB_CHUNK_SIZE):
                for vec in embed_engine.embed(texts[si: si + SUB_CHUNK_SIZE], batch_size=args.embed_batch):
                    vectors.append(matryoshka(vec, dim) if truncate else vec.tolist())
                    pbar.update(1)
                import gc; gc.collect()

        qdrant.upload_points(
            collection_name=args.collection,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vec,
                    payload={
                        "id":     p["track_id"],
                        "owner":  p["meta"].get("owner"),
                        "fields": p["fields"],
                        "meta":   {"manifestCid": p["meta"].get("manifestCid")},
                    }
                )
                for vec, p in zip(vectors, chunk)
            ],
            batch_size=256,
        )

        # Partial crash-recovery state: only records within the current manifest.
        # Cleared when the manifest completes (see callers).
        checkpoint["processed_track_ids"].extend(p["track_id"] for p in chunk)
        checkpoint["manifests"].update({
            p["meta"]["manifestCid"]: p["meta"].get("blockTimestamp")
            for p in chunk
        })
        _save_checkpoint(checkpoint, args.checkpoint_file)

        del texts, vectors
        import gc; gc.collect()

# ---------------------------------------------------------------------------
# PIPELINE EXECUTION
# ---------------------------------------------------------------------------
async def main():
    args = parse_args()

    qdrant = QdrantClient(
        host=args.qdrant_host, port=args.qdrant_port,
        grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=600
    )

    if args.umap_only:
        if not qdrant.collection_exists(args.collection):
            print(f"[Builder] collection '{args.collection}' does not exist — nothing to project")
            return
        write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist)
        ensure_indexes(qdrant, args.collection)
        return

    model_dim = MODEL_DIM_MAP.get(args.embedding_model, 768)
    dim       = min(args.dim, model_dim)
    truncate  = dim < model_dim

    checkpoint = _load_checkpoint(args.checkpoint_file)
    completed_manifest_cids = set(checkpoint["completed_manifest_cids"])
    # processed_track_ids: only non-empty when a previous run crashed mid-manifest
    processed_track_ids = set(checkpoint["processed_track_ids"])

    # ── BUNDLE PATH ──────────────────────────────────────────────────────────
    if args.bundle:
        b_name, b_id = args.bundle.split("=", 1)
        print(f"\n[Builder] Bundle mode: '{b_name.strip()}' (root={args.root_type})")

        if args.reset and qdrant.collection_exists(args.collection):
            print(f"[Builder] Resetting collection '{args.collection}'...")
            qdrant.delete_collection(args.collection)
            completed_manifest_cids.clear()
            processed_track_ids.clear()
            checkpoint["completed_manifest_cids"] = []
            checkpoint["processed_track_ids"] = []

        role_map: dict = {}
        if os.path.exists(args.role_map_file):
            with open(args.role_map_file) as f:
                role_map = json.load(f)

        # Lazy-init: don't load the model into GPU VRAM until we know there's
        # actual new work to do (avoids OOM when everything is already checkpointed).
        embed_engine = None
        any_new = False
        manifest_num = 0

        async for mcid, records in build_bundle_joined_data(
            args, b_id.strip(), args.root_type,
            completed_manifest_cids=completed_manifest_cids,
        ):
            new_records = [r for r in records if r["track_id"] not in processed_track_ids]
            if not new_records:
                # All records in this manifest were already embedded — mark complete.
                completed_manifest_cids.add(mcid)
                checkpoint["completed_manifest_cids"] = list(completed_manifest_cids)
                checkpoint["processed_track_ids"] = []
                processed_track_ids.clear()
                _save_checkpoint(checkpoint, args.checkpoint_file)
                continue

            # First manifest with real work: set up collection + model.
            if embed_engine is None:
                if not qdrant.collection_exists(args.collection):
                    qdrant.create_collection(
                        args.collection,
                        vectors_config=models.VectorParams(
                            size=dim, distance=models.Distance.COSINE, on_disk=True)
                    )
                ensure_indexes(qdrant, args.collection)
                embed_engine = _init_embed_engine(args)
                print(f"[Builder] model dim={model_dim}, output dim={dim} (truncate={truncate})")

            if not role_map:
                role_map = infer_roles([r["fields"] for r in new_records])
                _save_role_map(role_map, args.role_map_file)
                print(f"[Builder] Role map inferred and saved to {args.role_map_file}")

            manifest_num += 1
            print(f"[Builder] Manifest {manifest_num}: {_cid_to_path(mcid)[:16]}... — {len(new_records)} records")
            await _embed_and_upload(args, qdrant, embed_engine, new_records, role_map, dim, truncate, checkpoint)

            # Manifest fully committed: move to completed set and clear partial state.
            completed_manifest_cids.add(mcid)
            checkpoint["completed_manifest_cids"] = list(completed_manifest_cids)
            checkpoint["processed_track_ids"] = []
            processed_track_ids.clear()
            _save_checkpoint(checkpoint, args.checkpoint_file)
            any_new = True

        if not any_new:
            print("\n[Builder] No new bundle manifests to embed.")

    # ── LEGACY PATH (--schema / --primary) ───────────────────────────────────
    else:
        schemas = {}
        for pair in args.schemas:
            name, s_id = pair.split("=", 1)
            schemas[name.strip()] = s_id.strip()
        primary_key = args.primary or (next(iter(schemas)) if schemas else None)

        if not schemas:
            print("[Builder] No --schema provided and no --bundle. Nothing to do.")
            return
        if primary_key is None:
            print("[Builder] No --primary provided. Nothing to do.")
            return

        if args.reset and qdrant.collection_exists(args.collection):
            print(f"[Builder] Resetting collection '{args.collection}'...")
            qdrant.delete_collection(args.collection)

        if not qdrant.collection_exists(args.collection):
            qdrant.create_collection(
                args.collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE, on_disk=True)
            )
        ensure_indexes(qdrant, args.collection)
        embed_engine = _init_embed_engine(args)
        print(f"[Builder] model dim={model_dim}, output dim={dim} (truncate={truncate})")

        manifest_checkpoints = checkpoint.get("manifests", {})
        primary_records = []
        secondary_by_track: dict = {}

        for s_name, s_id in schemas.items():
            print(f"\n[Builder] [1/4] Querying Subgraph events for schema: {s_name}")
            publishes, updates = await _fetch_all_events_async(
                args.subgraph_url, args.graph_api_key, s_id, args.page_size
            )

            cids_meta: dict = {}
            for p in publishes: cids_meta[p["manifestCid"]] = p
            for u in updates:   cids_meta[u["manifestCid"]] = u

            print(f"[Builder] [2/4] Syncing manifests from IPFS Gateway...")
            _gw_h = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}
            manifests = await fetch_all_ipfs(
                list(cids_meta.keys()), args.ipfs_gateway, args.ipfs_timeout,
                args.concurrency, desc="Manifest Files", headers=_gw_h
            )

            data_cids_to_meta: dict = {}
            for c, json_data in manifests.items():
                if not json_data: continue
                for entry in json_data.get("entries", []):
                    dcid = entry.get("fields", {}).get("dataCid")
                    if dcid: data_cids_to_meta[dcid] = cids_meta[c]

            print(f"[Builder] [3/4] Pulling structural data payloads from IPFS...")
            payloads = await fetch_all_ipfs(
                list(data_cids_to_meta.keys()), args.ipfs_gateway, args.ipfs_timeout,
                args.concurrency, desc="Payload Data", headers=_gw_h
            )

            for dcid, data in payloads.items():
                if not data: continue
                records = data if isinstance(data, list) else [data]
                for r in records:
                    fields = r.get("fields", r) if isinstance(r, dict) else {}
                    t_id = _track_id(fields)
                    meta = data_cids_to_meta[dcid]
                    if s_name == primary_key:
                        primary_records.append({"track_id": t_id, "fields": fields, "meta": meta})
                    else:
                        secondary_by_track.setdefault(t_id, []).append(fields)

            for c, m in cids_meta.items():
                manifest_checkpoints[c] = m["blockTimestamp"]

        if not primary_records:
            print("\n[Builder] No new entries found.")
            if args.umap:
                write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist)
            return

        print(f"\n[Builder] Merging primary and secondary schemas on track ID keys...")
        joined_data = []
        skipped_count = 0
        for item in primary_records:
            t_id, fields, meta = item["track_id"], dict(item["fields"]), item["meta"]
            if t_id in processed_track_ids:
                skipped_count += 1
                continue
            if t_id in secondary_by_track:
                for sec_fields in secondary_by_track[t_id]: fields.update(sec_fields)
            joined_data.append({"track_id": t_id, "fields": fields, "meta": meta})
        print(f"[Builder] Skipped {skipped_count} already-checkpointed track IDs.")

        if not joined_data:
            print("\n[Builder] No new joined records to embed.")
            if args.umap:
                write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist)
            return

        role_map = infer_roles([j["fields"] for j in joined_data])
        _save_role_map(role_map, args.role_map_file)
        print(f"[Builder] Wrote global role map to {args.role_map_file}")

        checkpoint["manifests"] = manifest_checkpoints
        await _embed_and_upload(args, qdrant, embed_engine, joined_data, role_map, dim, truncate, checkpoint)

    # ── POST-PROCESSING ───────────────────────────────────────────────────────
    if args.umap:
        write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist)
    ensure_indexes(qdrant, args.collection)
    print("\n[Builder] All tasks complete.")


if __name__ == "__main__":
    asyncio.run(main())
