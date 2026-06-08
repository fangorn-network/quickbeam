# Fangorn Embeddings Builder and VectorDB Server

FastAPI + Qdrant read model for Fangorn manifests. Fetches published manifests from The Graph subgraph, resolves them from IPFS, joins across any number of schemas on a shared primary key (`trackId`), embeds via fastembed/ONNX, and serves a vector search API to the SOND3R client.

---

## How it works

```
Subgraph (events) → IPFS (manifests) → join on trackId → Qdrant → API
```

There are two separate processes: an offline **embeddings builder** (`embeddings.py`) that does the heavy GPU work and populates Qdrant, and a **server** (`server.py`) that reads from Qdrant and serves the API. The server does not ingest on startup — it expects the collection to already be populated, either by the builder or by seeding from an IPFS bundle.

Each schema is fetched independently and deduped (newest manifest wins per entry name). All schemas are joined on the primary schema's entry name. The merged field set is stored as a JSON payload in Qdrant; a searchable text document (built from inferred or configured fields) drives the embedding.

Responses return `{ id, fields: {...}, ...meta }` — the client reads `hit.fields.title`, not a parsed pipe-delimited string.

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
# Point the linker to your venv's nvidia libraries
export LD_LIBRARY_PATH=/path/to/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:/path/to/venv/lib/python3.12/site-packages/nvidia/cublas/lib:$LD_LIBRARY_PATH

python3 embeddings.py \
  -s test.sond3r.track.invariants.3=0xc4103f242a1e99bda3d6c484aa4e8155fc7e2df8fa6f59e0362a592b91570143 \
  -s test.sond3r.track.taxonomy.2=0x382fdaf1fb03f43ee0e5bcb0517fe0d2df3a3e9d27dddedf371c67e4812b6720 \
  --primary test.sond3r.track.invariants.3 \
  --graph-api-key <key> \
  --ipfs-gateway https://green-reasonable-heron-957.mypinata.cloud/ipfs
```

### 3. Start the server

```sh
python server.py \
  -s test.sond3r.track.invariants.3=0xc4103f242a1e99bda3d6c484aa4e8155fc7e2df8fa6f59e0362a592b91570143 \
  -s test.sond3r.track.taxonomy.2=0x382fdaf1fb03f43ee0e5bcb0517fe0d2df3a3e9d27dddedf371c67e4812b6720 \
  --primary test.sond3r.track.invariants.3 \
  --qdrant-host xyz-example.eu-central.aws.cloud.qdrant.io \
  --qdrant-port 6334 \
  --qdrant-api-key your-api-key  # need to add this arg
```

  python - << 'EOF'
  from qdrant_client import QdrantClient

  src = QdrantClient(host="localhost", port=6333, prefer_grpc=True)
  dst = QdrantClient(
      url="https://c741b23e-fe75-4f00-8f6e-53709c011371.us-east-1-1.aws.cloud.qdrant.io:6334",
      api_key="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIiwic3ViamVjdCI6ImFwaS1rZXk6NDVkZjI4Y2YtNDhiMS00ZGY2LWExMDYtNWZiMzBiNGE4NDQzIn0.Rp90WUQjOuw410DXWzgdTYEb-9Jw5Y5Ob_DjFYfJyZU",
  )

  src.migrate(dst, collection_names=["fangorn"])
  EOF

The server starts immediately and serves whatever is already in Qdrant. Use `POST /reingest` to pull new subgraph data without restarting.

---

## Seeding from a bundle

For distribution and local simulation — export the populated vector collection as an NDJSON bundle, pin it to IPFS, and seed any new instance from the CID on startup. No GPU required on the receiving end.

### Export

```sh
python export_bundle.py --src http://localhost:8080 --out bundle.ndjson
```

Pin `bundle.ndjson` to IPFS (e.g. via Pinata), grab the CID.

### Seed on startup

```sh
python server.py \
  -s test.sond3r.track.invariants.3=0x... \
  --bundle-cid QmYourBundleCIDHere
```

If the collection is empty and `--bundle-cid` is provided, the server fetches the NDJSON from IPFS and upserts it in the background. The server is live and queryable immediately — results populate as the seed progresses. If the collection already has points, the seed is skipped.

### Manual upsert

To seed a running instance without restarting:

```sh
# Stream from another server instance
curl -N http://host-a:8080/bundle/export \
  | curl -X POST http://host-b:8080/bundle/import \
       -H "Content-Type: application/x-ndjson" \
       --data-binary @-

# Or from a local file
cat bundle.ndjson | curl -X POST http://localhost:8080/bundle/import \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @-
```

---

## Configuration

All config is via CLI flags.

### Required

| Flag | Description |
|---|---|
| `--schema` / `-s` | `NAME=0x...` schema ID pair. Repeatable. |

### Optional

| Flag | Default | Description |
|---|---|---|
| `--primary` / `-p` | First schema listed | Schema whose entry names are the join key |
| `--subgraph-url` | Fangorn studio URL | The Graph subgraph endpoint |
| `--graph-api-key` | `""` | The Graph gateway API key |
| `--ipfs-gateway` | `https://gateway.pinata.cloud/ipfs` | IPFS gateway for manifest and bundle resolution |
| `--qdrant-host` | `localhost` | Qdrant host |
| `--qdrant-port` | `6333` | Qdrant HTTP port |
| `--qdrant-grpc-port` | `6334` | Qdrant gRPC port |
| `--collection` | `fangorn` | Qdrant collection name |
| `--checkpoint-file` | `./db/ingest_checkpoint.json` | Ingest progress file |
| `--searchable-fields` | `auto` | Comma-separated field allowlist, or `auto` to derive from inferred roles |
| `--embedding-model` | `nomic-ai/nomic-embed-text-v1.5` | fastembed model name |
| `--page-size` | `100` | Subgraph pagination page size |
| `--ipfs-timeout` | `20` | IPFS request timeout (seconds) |
| `--concurrency` | `16` | Max concurrent IPFS fetches |
| `--bundle-cid` | `None` | IPFS CID of an NDJSON bundle to seed from on first startup |
| `--host` | `0.0.0.0` | Server bind host |
| `--port` | `8080` | Server bind port |
| `--reset` | `false` | Delete and recreate the Qdrant collection on startup |

---

## API

### `GET /browse`

Paginated browse over all documents.

```
GET /browse?limit=20&offset=0
```

```json
{
  "results": [
    {
      "id": "abc123",
      "fields": {
        "trackId": "abc123",
        "title": "Do I Wanna Know?",
        "byArtist": "Arctic Monkeys",
        "genres": ["indie rock", "alternative"],
        "moods": ["melancholic", "longing"]
      },
      "owner": "0x..."
    }
  ],
  "total": 842
}
```

### `GET /search`

Semantic search by text query. Embeds the query server-side using the same model as ingestion.

```
GET /search?q=late+night+driving&n_results=10
GET /search?q=melancholic+indie&n_results=20&owner=0x...
```

Returns hits with `score` (0–1), `embedding`, and `fields`.

### `POST /search/vector`

Query by raw embedding vector — used by the ambient agent and Markov kernel.

```json
{
  "embedding": [0.12, -0.04, "..."],
  "n_results": 20,
  "owner": "0x..."
}
```

### `POST /search/text`

Lexical search over an in-memory index of title, subtitle, and tag fields. Faster than semantic search for exact name lookups.

```json
{ "q": "arctic monkeys", "limit": 20, "owner": "0x..." }
```

### `POST /embed`

Embed text via the same model used for ingestion — keeps client and server embedding spaces aligned.

```json
{ "text": "late night melancholic indie" }
→ { "embedding": [...] }

{ "texts": ["track one", "track two"] }
→ { "embeddings": [[...], [...]] }
```

### `GET /schema`

Returns the inferred semantic role map for the active dataset, plus facet vocabularies for tag fields.

```json
{
  "roles": { "title": "title", "subtitle": "byArtist", "tags": ["genres", "moods"] },
  "labels": { "title": "Title", "subtitle": "Artist" },
  "facets": { "genres": ["alternative", "indie rock", "..."], "moods": ["..."] },
  "primary_tag": "genres"
}
```

### `GET /catalog/map`

Returns a 2D UMAP projection of the entire collection for the CatalogGalaxy view. Computed on first request and cached. Returns `{ "computing": true }` if still running.

### `POST /catalog/map/refresh`

Invalidates the map cache and recomputes in the background.

### `GET /bundle/export`

Streams the full collection as NDJSON — one point per line, each with `track_id`, `fields`, `embedding`, `owner`, `meta`. No buffering; safe for large collections.

```
GET /bundle/export
GET /bundle/export?owner=0x...
GET /bundle/export?limit=1000&offset=0
```

### `POST /bundle/import`

Streaming NDJSON import. Reads line by line, upserts in batches of 500.

```sh
cat bundle.ndjson | curl -X POST http://localhost:8080/bundle/import \
  -H "Content-Type: application/x-ndjson" --data-binary @-
```

### `POST /bundle/upsert`

JSON body upsert for smaller programmatic use. Accepts pre-embedded points directly — no re-embedding on the server.

```json
{
  "points": [
    { "track_id": "abc123", "fields": {}, "embedding": [...], "owner": "0x...", "meta": {} }
  ]
}
```

### `GET /health`

```json
{
  "status": "ok",
  "count": 842,
  "schemas": { "core": "0x..." },
  "roles": { "..." },
  "map_cached": true,
  "text_indexed": true
}
```

### `POST /reingest`

Triggers a background re-ingestion from the subgraph without restarting. Existing points are upserted over; only changed documents are re-embedded.

### `POST /reingest/full`

Clears the checkpoint and re-ingests everything from scratch. Does not drop the Qdrant collection.

### `GET /debug`

Returns matched/unmatched track IDs across primary and secondary schemas, plus tag details. Useful for diagnosing join misses.

---

## Join semantics

- The `--primary` schema's entry names are the join key (`trackId`)
- All other schemas are indexed by entry name and merged into a single field dict
- On key conflicts, the secondary schema wins (enrichment pattern)
- If a secondary schema has multiple entries per `trackId`, they are merged left-to-right — last writer wins within that schema
- Entries in secondary schemas with no matching primary entry are silently dropped
- The semantic role map (`title`, `subtitle`, `tags`) is inferred automatically from field names and value shapes across the merged dataset

---

## Install

```sh
pip install fastapi uvicorn qdrant-client fastembed aiohttp requests umap-learn
``` 