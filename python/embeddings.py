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
from roles import infer_roles
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
    # ── Bundle mode: a single bundle schema whose v3 manifests carry typed
    #    node chunks + an edge chunk. When set, the builder walks committed
    #    edges to join instead of guessing a track-id join across --schema.
    #    Mutually exclusive in practice with the --schema/--primary path.
    parser.add_argument("--bundle", default=None,
                        help="Bundle schema as name=schemaId. Uses edge-walk join instead of --primary track-id join.")
    parser.add_argument("--root-type", default="Track",
                        help="Bundle root node type — one record is emitted per root node.")
    parser.add_argument("--subgraph-url", default="https://gateway.thegraph.com/api/subgraphs/id/8SgbhtiitpAhEfyTgeAHxHH5DQ2gTygUuXgc3b7MCFyc")
    parser.add_argument("--graph-api-key", default="")
    parser.add_argument("--ipfs-gateway", default="https://gateway.pinata.cloud/ipfs")
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
    parser.add_argument("--embed-batch", type=int, default=16, help="GPU embed batch size (lower for small VRAM)")
    parser.add_argument("--role-map-file", default="./db/role_map.json")
    # UMAP precompute. The 2D galaxy projection is catalog-wide and identical for
    # every user, so it is BUILDER work, computed once here and baked into the
    # snapshot as px/py payload — never recomputed on a client.
    parser.add_argument("--umap", action="store_true", default=False, help="Compute + store UMAP px/py after ingest")
    parser.add_argument("--umap-only", action="store_true", default=False, help="Skip ingest; only (re)compute UMAP on the existing collection")
    parser.add_argument("--umap-neighbors", type=int, default=15)
    parser.add_argument("--umap-min-dist", type=float, default=0.05)
    return parser.parse_args()

MODEL_DIM_MAP = {
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384
}

# ---------------------------------------------------------------------------
# MATRYOSHKA TRUNCATION
# nomic-embed-text-v1.5 recipe: layer-norm over the dim axis, slice to N,
# then L2-normalize. Run identically on the SOND3R query side or distances break.
# ---------------------------------------------------------------------------
def matryoshka(vec, dim):
    x = np.asarray(vec, dtype=np.float32)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-5)
    x = x[:dim]
    n = np.linalg.norm(x)
    return (x / n).astype(np.float32).tolist() if n else x.tolist()

# ---------------------------------------------------------------------------
# PAYLOAD INDEXES (idempotent — runs every build)
# Text indexes power lexical /search/text (exact artist/title lookup); the owner
# keyword index powers owner-filtered queries. These are METADATA, not vectors —
# creating them never re-embeds anything and is fast even on a full collection.
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
# UMAP PRECOMPUTE (one-time, builder-side)
# Pulls all vectors, projects to 2D, normalises to [-1, 1], and writes px/py
# back onto each point's payload via batched set-payload operations. The coords
# then travel inside the Qdrant snapshot, so the client renders the galaxy with
# zero compute.
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

    # --- Probe dims from first point before allocating anything ---
    probe, _ = qdrant.scroll(collection, limit=1, with_vectors=True, with_payload=False)
    if not probe:
        print("[umap] collection empty, nothing to project")
        return
    dims = len(probe[0].vector)

    # --- Allocate mmap up front; write directly during scroll ---
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
            arr[cursor] = p.vector   # write straight to disk-backed array
            cursor += 1
        pbar.update(len(pts))
        if offset is None:
            break
    pbar.close()

    n = cursor  # actual count (may differ from total if collection shifted)

    # --- Sample for fitting ---
    sample_size = min(30_000, n)
    print(f"[umap] fitting UMAP on {sample_size}-vector sample ...")
    indices = np.random.choice(n, sample_size, replace=False)
    train_sample = np.array(arr[indices], dtype=np.float32)  # small, fine in RAM

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

    # --- proj also mmap'd ---
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

    # --- Normalize in-place ---
    for ax in range(2):
        mn, mx = float(proj[:, ax].min()), float(proj[:, ax].max())
        rng = (mx - mn) or 1.0
        proj[:, ax] = (proj[:, ax] - mn) / rng * 2 - 1

    # --- Write back ---
    print("[umap] writing px/py back to payloads ...")
    B = 1000
    bad = 0
    for i in tqdm(range(0, n, B), desc="  ↳ set_payload", unit=" batch"):
        ops = []
        batch_proj = np.array(proj[i:i + B])  # force copy out of mmap into plain ndarray
        for pid, row in zip(ids[i:i + B], batch_proj):
            x, y = float(row[0]), float(row[1])
            if not (np.isfinite(x) and np.isfinite(y)):
                bad += 1
                x, y = 0.0, 0.0  # or skip — NaN/inf will always blow up gRPC
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
# SUBGRAPH & IPFS LOGIC
# ---------------------------------------------------------------------------
PUBLISHES_QUERY = "query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!) { manifestPublisheds(where: { schemaId: $schemaId }, first: $first, skip: $skip, orderBy: blockNumber, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
UPDATES_QUERY = "query Updates($schemaId: Bytes!, $first: Int!, $skip: Int!) { manifestUpdateds(where: { schemaId: $schemaId }, first: $first, skip: $skip, orderBy: version, orderDirection: desc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"

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

async def _fetch_all_events_async(url, api_key, schema_id, page_size):
    publishes, updates = [], []
    for target, query, key in [(publishes, PUBLISHES_QUERY, "manifestPublisheds"), (updates, UPDATES_QUERY, "manifestUpdateds")]:
        skip = 0
        pbar = tqdm(desc=f"  ↳ Fetching {key}", unit=" events", leave=False)
        while True:
            data = await _query_subgraph_async(url, api_key, query, {"schemaId": schema_id, "first": page_size, "skip": skip})
            batch = data.get(key, [])
            target.extend(batch)
            pbar.update(len(batch))
            if len(batch) < page_size: break
            skip += page_size
        pbar.close()
    return publishes, updates

async def _fetch_json(session, sem, url, timeout, pbar):
    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    res = json.loads(await resp.text())
                    pbar.update(1)
                    return res
        except Exception: pass
        pbar.update(1)
        return None

async def fetch_all_ipfs(cids, gateway, timeout, concurrency, desc="Downloading IPFS"):
    if not cids: return {}
    sem = asyncio.Semaphore(concurrency)
    pbar = tqdm(total=len(cids), desc=f"  ↳ {desc}", unit=" file")
    async with aiohttp.ClientSession() as session:
        tasks = []
        for cid in cids:
            url = f"{gateway.rstrip('/')}/{cid}"
            tasks.append(asyncio.create_task(_fetch_json(session, sem, url, timeout, pbar)))
        results = await asyncio.gather(*tasks)
    pbar.close()
    return dict(zip(cids, results))

def _track_id(fields: dict, prefer: str | None = None) -> str:
    # Bundle mode passes the root node's stable, publisher-assigned id here. It is
    # the canonical join key, so it wins over any heuristic derived from merged
    # fields (which can be clobbered by a neighbour's `id` after the edge-walk).
    if prefer:
        return str(prefer).strip().removeprefix("track:")
    for key in ["trackId", "track_id", "id", "contentId"]:
        if fields.get(key): return str(fields[key]).strip().removeprefix("track:")
    artist = str(fields.get("artist") or "").strip()
    title = str(fields.get("title") or "").strip()
    if artist and title: return hashlib.sha256(f"{artist}:{title}".encode()).hexdigest()[:24]
    return str(uuid.uuid4())[:12]

# ---------------------------------------------------------------------------
# BUNDLE JOIN  (edge-walk replacement for the track-id join)
#
# A bundle publishes ONE ManifestPublished event per dataset, pointing at a v3
# bundle manifest: { version:3, nodeChunks:[{type,dataCid,leaf}], edgeChunk:{dataCid,leaf} }.
# Node chunks are lists of { id, type, fields }; the edge chunk is a list of
# { rel, from, to } over node ids. We walk one hop from each root-type node and
# flatten its neighbors' fields INTO the root's fields — identical merge
# semantics to the legacy secondary_by_track join, so everything downstream
# (role inference, embedding text, Qdrant payload) runs unchanged.
#
# Output shape matches the legacy path exactly: [{ "track_id", "fields", "meta" }].
# ---------------------------------------------------------------------------
async def build_bundle_joined_data(args, schema_id, root_type):
    print(f"\n[Builder] [1/3] Querying Subgraph for bundle ManifestPublished events...")
    publishes, updates = await _fetch_all_events_async(
        args.subgraph_url, args.graph_api_key, schema_id, args.page_size
    )

    # newest manifest wins per CID; updates override publishes for the same cid
    cids_meta = {}
    for p in publishes: cids_meta[p["manifestCid"]] = p
    for u in updates: cids_meta[u["manifestCid"]] = u

    if not cids_meta:
        return []

    print(f"[Builder] [2/3] Fetching v3 bundle manifests from IPFS...")
    manifests = await fetch_all_ipfs(
        list(cids_meta.keys()), args.ipfs_gateway, args.ipfs_timeout,
        args.concurrency, desc="Bundle Manifests"
    )

    # collect every node-chunk + edge-chunk CID across all bundle manifests
    chunk_cids = set()
    manifest_chunks = {}  # manifestCid -> (node_cids, edge_cid)
    skipped_non_v3 = 0
    for mcid, m in manifests.items():
        if not m or m.get("version") != 3:
            skipped_non_v3 += 1
            continue
        node_cids = [c["dataCid"] for c in m.get("nodeChunks", [])]
        edge_cid = (m.get("edgeChunk") or {}).get("dataCid")
        if edge_cid is None:
            skipped_non_v3 += 1
            continue
        manifest_chunks[mcid] = (node_cids, edge_cid)
        chunk_cids.update(node_cids)
        chunk_cids.add(edge_cid)

    if skipped_non_v3:
        print(f"[Builder] Skipped {skipped_non_v3} manifests that were not valid v3 bundles.")
    if not manifest_chunks:
        return []

    print(f"[Builder] [3/3] Pulling node + edge chunks from IPFS...")
    chunks = await fetch_all_ipfs(
        list(chunk_cids), args.ipfs_gateway, args.ipfs_timeout,
        args.concurrency, desc="Bundle Chunks"
    )

    joined = []
    for mcid, (node_cids, edge_cid) in manifest_chunks.items():
        meta = cids_meta[mcid]

        # hydrate: index every node by id (ids unique across the whole bundle)
        nodes_by_id = {}
        for ncid in node_cids:
            for node in (chunks.get(ncid) or []):
                nodes_by_id[node["id"]] = node
        edges = chunks.get(edge_cid) or []

        # group outgoing edges once: (from_id, rel) -> [to_id, ...]
        out = {}
        for e in edges:
            out.setdefault((e["from"], e["rel"]), []).append(e["to"])

        # one record per root node, neighbors flattened into fields
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
                        fields.update(nb.get("fields", {}))  # same merge as secondary_by_track
            joined.append({
                "track_id": _track_id(fields, prefer=node.get("id")),
                "fields": fields,
                "meta": meta,
            })

    return joined

# ---------------------------------------------------------------------------
# PIPELINE EXECUTION
# ---------------------------------------------------------------------------
async def main():
    args = parse_args()

    qdrant = QdrantClient(host=args.qdrant_host, port=args.qdrant_port,
                      grpc_port=args.qdrant_grpc_port, prefer_grpc=True,
                      timeout=600)

    # ── UMAP-only mode: skip all ingest, just (re)project the existing collection
    if args.umap_only:
        if not qdrant.collection_exists(args.collection):
            print(f"[Builder] collection '{args.collection}' does not exist — nothing to project")
            return
        write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist)
        ensure_indexes(qdrant, args.collection)
        return

    schemas = {}
    for pair in args.schemas:
        name, s_id = pair.split("=", 1)
        schemas[name.strip()] = s_id.strip()
    primary_key = args.primary or (next(iter(schemas)) if schemas else None)

    # Init embedder. Provider list falls back to CPU so a missing/oversized CUDA
    # allocation degrades instead of crashing. Tune gpu_mem_limit to your card.
    embed_engine = TextEmbedding(
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

    model_dim = MODEL_DIM_MAP.get(args.embedding_model, 768)
    dim = min(args.dim, model_dim)
    truncate = dim < model_dim
    print(f"[Builder] model native dim={model_dim}, output dim={dim} (truncate={truncate})")

    if args.reset and qdrant.collection_exists(args.collection):
        print(f"[Builder] Resetting collection '{args.collection}'...")
        qdrant.delete_collection(args.collection)
    if not qdrant.collection_exists(args.collection):
        qdrant.create_collection(
            args.collection,
            vectors_config=models.VectorParams(
                size=dim, distance=models.Distance.COSINE, on_disk=True)
        )
    ensure_indexes(qdrant, args.collection)

    # Load checkpoint
    try:
        with open(args.checkpoint_file) as f:
            checkpoint = json.load(f)
            if "manifests" not in checkpoint:
                checkpoint = {"manifests": checkpoint, "processed_track_ids": []}
    except Exception:
        checkpoint = {"manifests": {}, "processed_track_ids": []}

    manifest_checkpoints = checkpoint.get("manifests", {})
    processed_track_ids = set(checkpoint.get("processed_track_ids", []))

    # ── JOIN PHASE ───────────────────────────────────────────────────────────
    # Two interchangeable ways to produce `joined_data` (shape: [{track_id, fields, meta}]).
    # Bundle mode walks committed edges; legacy mode joins secondaries on track id.
    # Everything AFTER this block is identical for both.

    if args.bundle:
        b_name, b_id = args.bundle.split("=", 1)
        print(f"\n[Builder] Bundle mode for '{b_name.strip()}' (root type = {args.root_type})")
        raw_joined = await build_bundle_joined_data(args, b_id.strip(), args.root_type)

        joined_data = []
        skipped_count = 0
        for item in raw_joined:
            if item["track_id"] in processed_track_ids:
                skipped_count += 1
                continue
            joined_data.append(item)
        print(f"[Builder] Skipped {skipped_count} track IDs already marked completed in checkpoint.")

        # record bundle manifest timestamps in the checkpoint
        for item in joined_data:
            manifest_checkpoints[item["meta"]["manifestCid"]] = item["meta"]["blockTimestamp"]

    else:
        if not schemas:
            print("[Builder] No --schema provided and no --bundle. Nothing to do.")
            return
        if primary_key is None:
            print("[Builder] No --primary provided. Nothing to do.")
            return

        primary_records = []
        secondary_by_track = {}

        # Extract Data Phase
        for s_name, s_id in schemas.items():
            print(f"\n[Builder] [1/4] Querying Subgraph events for schema: {s_name}")
            publishes, updates = await _fetch_all_events_async(args.subgraph_url, args.graph_api_key, s_id, args.page_size)

            cids_meta = {}
            for p in publishes: cids_meta[p["manifestCid"]] = p
            for u in updates: cids_meta[u["manifestCid"]] = u

            new_cids = list(cids_meta.keys())

            print(f"[Builder] [2/4] Syncing manifests from IPFS Gateway...")
            manifests = await fetch_all_ipfs(new_cids, args.ipfs_gateway, args.ipfs_timeout, args.concurrency, desc="Manifest Files")

            data_cids_to_meta = {}
            for c, json_data in manifests.items():
                if not json_data: continue
                for entry in json_data.get("entries", []):
                    dcid = entry.get("fields", {}).get("dataCid")
                    if dcid: data_cids_to_meta[dcid] = cids_meta[c]

            print(f"[Builder] [3/4] Pulling structural data payloads from IPFS...")
            payloads = await fetch_all_ipfs(list(data_cids_to_meta.keys()), args.ipfs_gateway, args.ipfs_timeout, args.concurrency, desc="Payload Data")

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
            print("\n[Builder] No new entries found. Computing UMAP if requested...")
            if args.umap:
                write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist)
            return

        # Cross-Schema Join Phase
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

        print(f"[Builder] Skipped {skipped_count} track IDs already marked completed in checkpoint.")

    # ── CONVERGED PATH (identical for bundle + legacy) ───────────────────────

    if not joined_data:
        print("\n[Builder] No new joined records to embed.")
        if args.umap:
            write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist)
        return

    role_map = infer_roles([j["fields"] for j in joined_data])

    rm_dir = os.path.dirname(args.role_map_file)
    if rm_dir and not os.path.exists(rm_dir):
        os.makedirs(rm_dir, exist_ok=True)
    with open(args.role_map_file, "w") as f:
        json.dump(role_map, f)
    print(f"[Builder] Wrote global role map to {args.role_map_file}")

    # ── Embedding Generation & Chunked Upload Phase ──────────────────────────
    print(f"\n[Builder] [4/4] Computing ONNX Embeddings via CUDA GPU Pipeline...")
    SAVE_BATCH_SIZE = 5000

    for i in range(0, len(joined_data), SAVE_BATCH_SIZE):
        chunk = joined_data[i : i + SAVE_BATCH_SIZE]
        print(f"\n[Execution] Processing Batch {i // SAVE_BATCH_SIZE + 1} ({len(chunk)} tracks)...")

        texts, points = [], []
        for item in chunk:
            fields = item["fields"]
            if args.searchable_fields == "auto":
                tags = " ".join(fields.get(t, "") if isinstance(fields.get(t), str) else "" for t in role_map.get("tags", []))
                text_str = f"Title: {fields.get(role_map.get('title',''), '')}. Tags: {tags}"
            else:
                text_str = " ".join(str(fields[k]) for k in args.searchable_fields.split(",") if fields.get(k))
            if len(text_str) > 1000:
                text_str = text_str[:1000]
            texts.append(f"search_document: {text_str}")
            points.append(item)

        vectors = []
        SUB_CHUNK_SIZE = 1000
        with tqdm(total=len(texts), desc="  ↳ GPU Vectors Generated", unit=" doc") as pbar:
            for sub_idx in range(0, len(texts), SUB_CHUNK_SIZE):
                sub_texts = texts[sub_idx : sub_idx + SUB_CHUNK_SIZE]
                for vec in embed_engine.embed(sub_texts, batch_size=args.embed_batch):
                    vectors.append(matryoshka(vec, dim) if truncate else vec.tolist())
                    pbar.update(1)
                import gc; gc.collect()

        # Minimal payload: join key + owner + display fields + provenance cid.
        qdrant_payload = [
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "id": p["track_id"],
                    "owner": p["meta"].get("owner"),
                    "fields": p["fields"],
                    "meta": {"manifestCid": p["meta"].get("manifestCid")},
                }
            ) for vec, p in zip(vectors, points)
        ]

        print(f"[Builder] Uploading {len(qdrant_payload)} points to Qdrant...")
        qdrant.upload_points(collection_name=args.collection, points=qdrant_payload, batch_size=256)

        for p in points:
            checkpoint["processed_track_ids"].append(p["track_id"])
        for p in points:
            m_cid = p["meta"]["manifestCid"]
            manifest_checkpoints[m_cid] = p["meta"]["blockTimestamp"]
        checkpoint["manifests"] = manifest_checkpoints

        checkpoint_dir = os.path.dirname(args.checkpoint_file)
        if checkpoint_dir and not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir, exist_ok=True)
        with open(args.checkpoint_file, "w") as f:
            json.dump(checkpoint, f)
        print(f"[Builder] Batch {i // SAVE_BATCH_SIZE + 1} committed to checkpoint.")

        del texts, points, vectors, qdrant_payload
        import gc; gc.collect()

    print("\n[Builder] Embedding complete.")

    # ── UMAP precompute (one-time). Run AFTER all points exist so the projection
    #    covers the whole catalog. Snapshot the collection after this to ship it.
    if args.umap:
        write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist)

    ensure_indexes(qdrant, args.collection)
    print("\n[Builder] All tasks complete.")

if __name__ == "__main__":
    asyncio.run(main())