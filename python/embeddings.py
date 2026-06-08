import certifi
import os
import io
import sys
import argparse
import asyncio
import aiohttp
import random
import hashlib
import json
import uuid

from qdrant_client import QdrantClient
from qdrant_client import models
from fastembed import TextEmbedding
from roles import infer_roles
from tqdm import tqdm  # Real-time telemetry engine

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
    return parser.parse_args()

MODEL_DIM_MAP = {
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384
}

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

def _track_id(fields: dict) -> str:
    for key in ["trackId", "track_id", "id", "contentId"]:
        if fields.get(key): return str(fields[key]).strip().removeprefix("track:")
    artist = str(fields.get("artist") or "").strip()
    title = str(fields.get("title") or "").strip()
    if artist and title: return hashlib.sha256(f"{artist}:{title}".encode()).hexdigest()[:24]
    return str(uuid.uuid4())[:12]

# ---------------------------------------------------------------------------
# PIPELINE EXECUTION
# ---------------------------------------------------------------------------
async def main():
    args = parse_args()
    schemas = {}
    for pair in args.schemas:
        name, s_id = pair.split("=", 1)
        schemas[name.strip()] = s_id.strip()
    primary_key = args.primary or next(iter(schemas))
    
    # Init Clients
    qdrant = QdrantClient(host=args.qdrant_host, port=args.qdrant_port, grpc_port=args.qdrant_grpc_port, prefer_grpc=True)
    # Find this initialization in your script:
    # Deep optimization for the ONNX BFC Memory Arena
    embed_engine = TextEmbedding(
        model_name=args.embedding_model,
        max_length=512,
        providers=[
            ("CUDAExecutionProvider", {
                "device_id": 0,
                "arena_extend_strategy": "kSameAsRequested", 
                "gpu_mem_limit": 3   * 1024 * 1024 * 1024,     
                "cudnn_conv_algo_search": "DEFAULT",
                
                # CHANGE THESE TWO LINE CONFIGURATIONS:
                "do_copy_in_default_stream": "True",
                "has_user_compute_stream": "False" # Forces predictable synchronous scheduling
            })
        ]
    )
    dim = MODEL_DIM_MAP.get(args.embedding_model, 768)

    if args.reset and qdrant.collection_exists(args.collection):
        print(f"[Builder] Resetting collection '{args.collection}'...")
        qdrant.delete_collection(args.collection)
    if not qdrant.collection_exists(args.collection):
        qdrant.create_collection(args.collection, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))

    # Load Checkpoint with backwards compatibility for track tracking
    try:
        with open(args.checkpoint_file) as f:
            checkpoint = json.load(f)
            if "manifests" not in checkpoint:
                checkpoint = {"manifests": checkpoint, "processed_track_ids": []}
    except Exception:
        checkpoint = {"manifests": {}, "processed_track_ids": []}

    manifest_checkpoints = checkpoint.get("manifests", {})
    processed_track_ids = set(checkpoint.get("processed_track_ids", []))

    primary_records = []
    secondary_by_track = {}

    # Extract Data Phase
    for s_name, s_id in schemas.items():
        print(f"\n[Builder] [1/4] Querying Subgraph events for schema: {s_name}")
        publishes, updates = await _fetch_all_events_async(args.subgraph_url, args.graph_api_key, s_id, args.page_size)
        
        cids_meta = {}
        for p in publishes: cids_meta[p["manifestCid"]] = p
        for u in updates: cids_meta[u["manifestCid"]] = u

        # # Unnest the actual dictionary if it wrapped in the "manifests" key
        # if isinstance(manifest_checkpoints, dict) and "manifests" in manifest_checkpoints:
        #     manifest_checkpoints = manifest_checkpoints["manifests"]

        # # Your fixed line 172:
        # new_cids = [c for c, m in cids_meta.items() if int(manifest_checkpoints.get(c, -1)) < int(m["blockTimestamp"])]
        # new_cids = [c for c, m in cids_meta.items() if int(manifest_checkpoints.get(c, -1)) < int(m["blockTimestamp"])]
        new_cids = list(cids_meta.keys())

        print(f"[Builder] [2/4] Syncing manifests from IPFS Gateway...")
        manifests = await fetch_all_ipfs(new_cids, args.ipfs_gateway, args.ipfs_timeout, args.concurrency, desc="Manifest Files")

        # Harvest Data CIDs
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
        print("\n[Builder] Success! No new entries found to process.")
        return

    # Cross-Schema Join & Role inference Phase
    print(f"\n[Builder] Merging primary and secondary schemas on track ID keys...")
    joined_data = []
    skipped_count = 0
    for item in primary_records:
        t_id, fields, meta = item["track_id"], dict(item["fields"]), item["meta"]
        
        # Track Resumability skipping mechanism
        if t_id in processed_track_ids:
            skipped_count += 1
            continue

        if t_id in secondary_by_track:
            for sec_fields in secondary_by_track[t_id]: fields.update(sec_fields)
        joined_data.append({"track_id": t_id, "fields": fields, "meta": meta})

    print(f"[Builder] Skipped {skipped_count} track IDs already marked completed in checkpoint.")

    if not joined_data:
        print("\n[Builder] All discovered tracks in these chunks are already uploaded.")
        return

    role_map = infer_roles([j["fields"] for j in joined_data])

    # ---------------------------------------------------------------------------
    # Embedding Generation & Chunked Upload Phase
    # ---------------------------------------------------------------------------
    print(f"\n[Builder] [4/4] Computing ONNX Embeddings via CUDA GPU Pipeline...")
    
    # Process in chunks of 5000 to save state frequently and prevent memory bloating
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
            
            # handle any unusually long strings
            if len(text_str) > 1000:
                text_str = text_str[:1000]
            texts.append(f"search_document: {text_str}")
            points.append(item)

        # Generate vectors for this chunk
        vectors = []
        # Breaks the 5k batches down to reset generator hooks
        SUB_CHUNK_SIZE = 1000 
        
        with tqdm(total=len(texts), desc="  ↳ GPU Vectors Generated", unit=" doc") as pbar:
            for sub_idx in range(0, len(texts), SUB_CHUNK_SIZE):
                sub_texts = texts[sub_idx : sub_idx + SUB_CHUNK_SIZE]
                
                # Consume the generator completely into a temporary pool
                for vec in embed_engine.embed(sub_texts, batch_size=32):    
                    vectors.append(vec.tolist())
                    pbar.update(1)
                
                # Explicit micro-flush mid-batch
                import gc
                gc.collect()
                
                # --- ADD A TINY THERMAL/POWER BREAK HERE ---
                import time
                time.sleep(0.1) # 100ms pause to let the GPU stabilize
                # -------------------------------------------

        # Package current chunk for Qdrant
        print(f"[Builder] Packaging vector collections into storage structures...")
        qdrant_payload = [
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=vec,
                payload={"id": p["track_id"], "owner": p["meta"]["owner"], "fields": p["fields"], "meta": p["meta"], "role_map_cache": role_map}
            ) for vec, p in zip(vectors, points)
        ]

        # Upload chunk
        print(f"[Builder] Shifting vectors to Qdrant Core Engine storage...")
        qdrant.upload_points(collection_name=args.collection, points=qdrant_payload, batch_size=1000)
        

        # IMMEDIATELY write progress for this chunk to the database checkpoint
        for p in points:
            checkpoint["processed_track_ids"].append(p["track_id"])

        # Add this here to update manifest tracking safely:
        for p in points:
            m_cid = p["meta"]["manifestCid"]
            manifest_checkpoints[m_cid] = p["meta"]["blockTimestamp"]

        checkpoint["manifests"] = manifest_checkpoints  

        # Make sure directory exists before saving
        checkpoint_dir = os.path.dirname(args.checkpoint_file)
        if checkpoint_dir and not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir, exist_ok=True)

        with open(args.checkpoint_file, "w") as f: 
            json.dump(checkpoint, f)
            
        print(f"[Builder] Batch {i // SAVE_BATCH_SIZE + 1} safely committed to checkpoint file.")
        del texts
        del points
        del vectors
        del qdrant_payload
        import gc
        gc.collect()

    print("\n[Builder] All tasks complete! Vector space database generation successfully updated.")

if __name__ == "__main__":
    asyncio.run(main())