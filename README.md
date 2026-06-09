  # fangorn-embeddings

Fangorn infrastructure for building and serving vector search over Fangorn data sources. Pulls manifests from The Graph, resolves payloads from IPFS, joins across schemas, embeds via fastembed/ONNX, and serves a semantic search API backed by Qdrant.

This is protocol infrastructure — not application code. Any schema owner can run this pipeline against their own Fangorn data sources. SOND3R is the first consumer.

---

## Architecture

```
Subgraph (events)
      ↓
IPFS (manifests + payloads)
      ↓
join on primary key (trackId)
      ↓
fastembed / ONNX (GPU)
      ↓
Qdrant
      ↓
FastAPI vector search API
```

Two separate processes:

- **`embeddings.py`** — offline builder. Pulls from subgraph, resolves IPFS, joins schemas, embeds, writes to Qdrant. Run this once (or on a schedule) on a machine with a GPU.
- **`server.py`** — read-only API server. Connects to Qdrant and serves search, browse, and catalog endpoints. Does not ingest on startup.

---

## Quickstart

### 1. Run Qdrant

```sh
docker run -d -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/qdrant_storage:/qdrant/storage:z" \
  --name qdrant-core \
  qdrant/qdrant
```

### 2. Build embeddings

```sh
# Link NVIDIA libraries if using CUDA
export LD_LIBRARY_PATH=/path/to/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:\
/path/to/venv/lib/python3.12/site-packages/nvidia/cublas/lib:$LD_LIBRARY_PATH

python3 embeddings.py \
  -s test.sond3r.track.invariants.3=0xc4103f242a1e99bda3d6c484aa4e8155fc7e2df8fa6f59e0362a592b91570143 \
  -s test.sond3r.track.taxonomy.2=0x382fdaf1fb03f43ee0e5bcb0517fe0d2df3a3e9d27dddedf371c67e4812b6720 \
  --primary test.sond3r.track.invariants.3 \
  --graph-api-key <key> \
  --ipfs-gateway https://your-gateway.mypinata.cloud/ipfs \
  --dim 256 \
  --umap \
  --reset <--- only if you want a full rebuild 
```

To only build UMAP coords, run

``` sh 
python embeddings.py --collection fangorn --umap-only
```



### 3. Start the server

```sh
python server.py \
  -s test.sond3r.track.invariants.3=0xc4103f242a1e99bda3d6c484aa4e8155fc7e2df8fa6f59e0362a592b91570143 \
  -s test.sond3r.track.taxonomy.2=0x382fdaf1fb03f43ee0e5bcb0517fe0d2df3a3e9d27dddedf371c67e4812b6720 \
  --primary test.sond3r.track.invariants.3 \
  --graph-api-key <key> \
  --ipfs-gateway https://your-gateway.mypinata.cloud/ipfs
```

The server starts immediately and serves whatever is already in Qdrant. Use `POST /reingest` to pull new subgraph data without restarting.

---

## Publishing a snapshot

``` sh
docker cp qdrant-core:/qdrant/snapshots/fangorn/fangorn-7445347200924990-2026-06-08-17-26-31.snapshot ~/fangorn.snapshot
```

Snapshots are versioned, derived artifacts registered on Fangorn. A snapshot captures the embedding bundle at a specific point in time, tied to specific source schema versions and a specific model. Clients use the snapshot CID to seed a local Qdrant instance without re-embedding.

Two IPFS artifacts are produced per snapshot:

- **Full bundle** (`bundle.ndjson`) — `track_id`, `fields`, `embedding` per line. Used to bootstrap a complete server instance.
- **Embeddings only** (`embeddings.ndjson`) — `track_id`, `embedding` per line. Minimal artifact for clients that only need the vector space.

### 1. Export

```sh
# Full bundle (fields + embeddings)
python export_bundle.py --src http://localhost:8080 --out bundle.ndjson

# Embeddings only (trackId + vector)
python export_bundle.py --src http://localhost:8080 --out embeddings.ndjson --embeddings-only
```

### 2. Pin to IPFS

Upload both files to Pinata (or any IPFS pinning service) and note the CIDs.

``` sh
# Compress the bundle
gzip -k python/bundle.ndjson

# Upload the compressed bundle
PINATA_JWT=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySW5mb3JtYXRpb24iOnsiaWQiOiJhNWFiOTAzNC04NDZmLTQ0YTMtOWUxMy1iYzViMGY4NGZhNWIiLCJlbWFpbCI6ImRyaWVtd29ya3NAZmFuZ29ybi5uZXR3b3JrIiwiZW1haWxfdmVyaWZpZWQiOnRydWUsInBpbl9wb2xpY3kiOnsicmVnaW9ucyI6W3siZGVzaXJlZFJlcGxpY2F0aW9uQ291bnQiOjEsImlkIjoiRlJBMSJ9LHsiZGVzaXJlZFJlcGxpY2F0aW9uQ291bnQiOjEsImlkIjoiTllDMSJ9XSwidmVyc2lvbiI6MX0sIm1mYV9lbmFibGVkIjpmYWxzZSwic3RhdHVzIjoiQUNUSVZFIn0sImF1dGhlbnRpY2F0aW9uVHlwZSI6InNjb3BlZEtleSIsInNjb3BlZEtleUtleSI6IjU2OTQ4OGI2Nzg2NTIxMGEzNGVmIiwic2NvcGVkS2V5U2VjcmV0IjoiMGQ1ZjE1ODMxOWViYzJlMDhhYjE0OWE1ZWE5MTRhZTQ4YzRkNWQwOTIzMDgyMGMyMmZhMDY2ZWE0ZTcwMGY2MyIsImV4cCI6MTgxMjQwNzI2MX0.6VBV4xJyk__73a4iTOBfyUkEasXQ_uFCwVcxDE6qSDU \
PINATA_GATEWAY=https://green-reasonable-heron-957.mypinata.cloud \
node src/pin.mjs bundle.ndjson.gz "sond3r-bundle-v1"
```

### 3. Publish the snapshot to Fangorn

```sh
npx tsx publish_embeddings.ts \
  --bundle-cid QmFullBundleCID \
  --embeddings-cid QmEmbeddingsOnlyCID \
  --block 19284710 \
  --total 1000000 \
  --schema sond3r.embeddings.0 \
  --dataset sond3r.embeddings.snapshot.1 \
  --source "test.sond3r.track.invariants.3=0xc4103f...=19280001" \
  --source "test.sond3r.track.taxonomy.2=0x382fda...=19279843"
```

Use `--dry-run` to inspect the record without publishing.

### Snapshot schema

The `sond3r.embeddings.0` schema definition:

```json
{
  "model":          { "@type": "string" },
  "dimensions":     { "@type": "number" },
  "createdAtBlock": { "@type": "number" },
  "sourceSchemas": {
    "@type": "array",
    "items": {
      "name":        { "@type": "string" },
      "schemaId":    { "@type": "string" },
      "latestBlock": { "@type": "number" }
    }
  },
  "dataCid":       { "@type": "string" },
  "embeddingsCid": { "@type": "string" },
  "totalCount":    { "@type": "number" }
}
```

Register it once:

```sh
fangorn schema register sond3r.embeddings.0
```

---

## Seeding from a snapshot

Pass a bundle CID to the server on startup. If the Qdrant collection is empty, the server fetches and upserts the bundle in the background — no GPU required on the receiving end.

```sh
python server.py \
  -s test.sond3r.track.invariants.3=0x... \
  --bundle-cid QmFullBundleCID
```

The server is live and queryable immediately. Results populate as the seed progresses. If the collection already has points, the seed is skipped.

### Manual import

```sh
# From a local file
cat bundle.ndjson | curl -X POST http://localhost:8080/bundle/import \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @-

# Stream directly between two server instances
curl -N http://host-a:8080/bundle/export \
  | curl -X POST http://host-b:8080/bundle/import \
       -H "Content-Type: application/x-ndjson" \
       --data-binary @-
```

---

## Migrating to Qdrant Cloud

```sh
python - << 'EOF'
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

src = QdrantClient(host="localhost", port=6334, prefer_grpc=True)
dst = QdrantClient(
    url="https://your-cluster.cloud.qdrant.io:6334",
    api_key="your-api-key",
    prefer_grpc=True,
)

COLLECTION = "fangorn"
BATCH      = 100

src_info = src.get_collection(COLLECTION)
if not dst.collection_exists(COLLECTION):
    dst.create_collection(COLLECTION, vectors_config=src_info.config.params.vectors)

offset = None
total  = 0
while True:
    records, next_offset = src.scroll(
        collection_name=COLLECTION,
        limit=BATCH,
        offset=offset,
        with_payload=True,
        with_vectors=True,
    )
    if not records:
        break
    dst.upsert(
        collection_name=COLLECTION,
        points=[PointStruct(id=pt.id, vector=pt.vector, payload=pt.payload) for pt in records],
        wait=True,
    )
    total += len(records)
    print(f"  {total} points migrated", end="\r", flush=True)
    if next_offset is None:
        break
    offset = next_offset

print(f"\ndone — {total} points")
EOF
```

Then point the server at the cloud cluster:

```sh
python server.py \
  -s ... \
  --qdrant-url https://your-cluster.cloud.qdrant.io:6334 \
  --qdrant-api-key your-api-key
```

---

## Configuration

### `embeddings.py`

| Flag | Default | Description |
|---|---|---|
| `--schema` / `-s` | required | `NAME=0x...` schema ID pair. Repeatable. |
| `--primary` / `-p` | first schema | Join key schema |
| `--subgraph-url` | Fangorn studio URL | The Graph endpoint |
| `--graph-api-key` | `""` | The Graph gateway API key |
| `--ipfs-gateway` | Pinata | IPFS gateway |
| `--qdrant-host` | `localhost` | Qdrant host |
| `--qdrant-port` | `6333` | Qdrant HTTP port |
| `--qdrant-grpc-port` | `6334` | Qdrant gRPC port |
| `--collection` | `fangorn` | Qdrant collection name |
| `--checkpoint-file` | `./db/ingest_checkpoint.json` | Resume state |
| `--embedding-model` | `nomic-ai/nomic-embed-text-v1.5` | fastembed model |
| `--reset` | `false` | Wipe collection and checkpoint before building |

### `server.py`

| Flag | Default | Description |
|---|---|---|
| `--schema` / `-s` | required | `NAME=0x...` schema ID pair. Repeatable. |
| `--primary` / `-p` | first schema | Join key schema |
| `--subgraph-url` | Fangorn studio URL | The Graph endpoint |
| `--graph-api-key` | `""` | The Graph gateway API key |
| `--ipfs-gateway` | Pinata | IPFS gateway for bundle and manifest resolution |
| `--qdrant-url` | `None` | Qdrant Cloud URL (overrides host/port) |
| `--qdrant-api-key` | `None` | Qdrant Cloud API key |
| `--qdrant-host` | `localhost` | Qdrant host (local) |
| `--qdrant-port` | `6333` | Qdrant HTTP port |
| `--qdrant-grpc-port` | `6334` | Qdrant gRPC port |
| `--collection` | `fangorn` | Qdrant collection name |
| `--embedding-model` | `nomic-ai/nomic-embed-text-v1.5` | Must match builder |
| `--bundle-cid` | `None` | IPFS CID to seed from on first startup |
| `--searchable-fields` | `auto` | Field allowlist or `auto` |
| `--host` | `0.0.0.0` | Bind host |
| `--port` | `8080` | Bind port |
| `--reset` | `false` | Drop and recreate collection on startup |

---

## API

### `GET /browse`
Paginated browse. `?limit=20&offset=0`

### `GET /search`
Semantic search. `?q=late+night+driving&n_results=10&owner=0x...`
Returns hits with `score`, `embedding`, `fields`.

### `POST /search/vector`
Query by raw embedding vector.
```json
{ "embedding": [...], "n_results": 20, "owner": "0x..." }
```

### `POST /search/text`
Lexical search over an in-memory index. Faster for exact name lookups.
```json
{ "q": "arctic monkeys", "limit": 20 }
```

### `POST /embed`
Embed text using the same model as ingestion.
```json
{ "text": "late night melancholic indie" }
→ { "embedding": [...] }
```

### `GET /schema`
Inferred semantic role map and facet vocabularies for the active dataset.

### `GET /catalog/map`
2D UMAP projection of the full collection. Computed on first request, cached. Returns `{ "computing": true }` while running.

### `POST /catalog/map/refresh`
Invalidate and recompute the map cache in the background.

### `GET /bundle/export`
Stream the full collection as NDJSON. `?owner=0x...&limit=1000&offset=0`

### `POST /bundle/import`
Streaming NDJSON import, upserts in batches of 500.

### `POST /bundle/upsert`
JSON body upsert for pre-embedded points.

### `GET /health`
Collection count, role map, cache state, checkpoint info.

### `POST /reingest`
Background re-ingestion from subgraph. Only re-embeds changed documents.

### `POST /reingest/full`
Clear checkpoint and re-ingest everything from scratch.

### `GET /debug`
Join diagnostics — matched/unmatched track IDs across primary and secondary schemas.

---

## Join semantics

- `--primary` schema entry names are the join key
- Secondary schemas are merged into the primary field set; secondary wins on conflict (enrichment pattern)
- Multiple secondary entries per key are merged left-to-right; last writer wins within that schema
- Secondary entries with no matching primary entry are dropped
- The semantic role map (`title`, `subtitle`, `tags`) is inferred automatically from field names and value shapes

---

## Install

```sh
pip install fastapi uvicorn qdrant-client fastembed aiohttp requests umap-learn
npm install -g tsx  # for publish_embeddings.ts
```# embeddings
