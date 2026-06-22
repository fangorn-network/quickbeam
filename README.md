# quickbeam

This repo contains infrastructure for building and serving vector search over on-chain data sources registered with [Fangorn](https://github.com/fangorn-network/fangorn). The core script pulls manifests from The Graph, resolves payloads from IPFS, joins across schemas, and then builds embeddings via fastembed/ONNX.

> **Two meanings of "bundle".** This doc uses the word in two unrelated ways:
> - **Schema bundle** (`--bundle`) — a registered subgraph schema whose v3 manifests carry typed node chunks plus an edge chunk. The builder walks those edges to join records.
> - **Snapshot bundle** (`--bundle-cid`, `/bundle/*`) — an exported NDJSON copy of the populated Qdrant collection, used to seed new instances without a GPU.

---

## How it works

- **`quickbeam build`**: offline (local, trusted) embeddings builder. Pulls from subgraph, resolves IPFS, joins schemas, embeds, writes to Qdrant.
- **`quickbeam watch`**: live daemon that polls the subgraph for new events and embeds them automatically as they arrive. Keeps the GPU model loaded between cycles; uses `blockNumber_gt` to only query genuinely new events.
- **`quickbeam serve`**: read-only API server. Connects to Qdrant and serves search, browse, and catalog endpoints. It does not ingest on startup, but instead expects the collection to already be populated, either by the builder or by seeding from a snapshot. Can optionally run the watcher alongside it (`serve --watch`) so one process both ingests and serves, and can gate the search routes behind [x402 payments](#x402-payment-gating).
- **`quickbeam mcp`**: a [Model Context Protocol](#mcp-server) layer over the API. A thin, stateless HTTP client of `quickbeam serve` that exposes semantic search to agents as well-typed tools, attaches on-chain provenance to every result, and can optionally charge the calling agent per tool call via [x402](#x402-payment-gating).
- **`quickbeam cdn` + `quickbeam pull`**: the [Semantic CDN](#semantic-cdn) — instead of running queries on the server (where the node sees every query = intent), the operator *bakes* the embedded graph into immutable, content-addressed shard files (a "domain") and *serves* them as static, resumable downloads. A user *pulls* a domain into their own local Qdrant and queries it offline. Knowledge moves to the user; the network never sees a query. See [docs/SEMANTIC_CDN.md](docs/SEMANTIC_CDN.md).

The builder produces the same record shape — `{ track_id, fields, meta }` — through one of two interchangeable join phases:

- **Flat schemas (`--schema`/`--primary`)** — schemas are fetched independently, deduped (newest manifest wins per entry name), then joined on the primary schema's entry name (`trackId`).
- **Schema bundle (`--bundle`)** — a single bundle schema publishes v3 manifests carrying typed node chunks (`{id, type, fields}`) and an edge chunk (`{rel, from, to}`). The builder walks outgoing edges from each root-type node and flattens neighbor fields into the root record — no track-id guessing.

Everything downstream (role inference, embedding text, Qdrant payload) is identical for both join modes.

---

## Installation

```sh
# From the repo root
python -m venv venv
source venv/bin/activate

pip install -e ".[gpu]"   # CUDA-accelerated embeddings (recommended for build)
pip install -e ".[cpu]"   # CPU-only fallback
```

This installs the `quickbeam` CLI entry point. Run `quickbeam --help` to see all commands.

```
quickbeam build    Build embeddings from subgraph / IPFS data into Qdrant
quickbeam watch    Live daemon: poll subgraph for new events and embed automatically
quickbeam serve    Start the Fangorn search API server (optionally with --watch + x402)
quickbeam mcp      Run the MCP server exposing search as agent tools (x402-aware)
quickbeam cdn      Semantic CDN: bake the embedded graph into static, pullable domain shards
quickbeam pull     Pull a domain from a Semantic CDN into a local Qdrant collection
quickbeam export   Export the Qdrant collection as an NDJSON bundle
quickbeam migrate  Migrate a local Qdrant collection to Qdrant Cloud
quickbeam data     Generate seed / test data from public data sources
```

The `mcp` and x402 layers need extra dependencies (FastMCP + EIP-712 signing):

```sh
pip install -e ".[agent]"   # fastmcp + eth-account + httpx
pip install -e ".[dev]"     # pytest + fastmcp + eth-account (to run the test-suite)
```

---

## Quickstart

### 1. Run Qdrant

```sh
docker run -d -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/python/qdrant_storage:/qdrant/storage:z" \
  --name qdrant-core \
  qdrant/qdrant
```

### 2. Build embeddings

Link the NVIDIA libraries if using CUDA:

```sh
export LD_LIBRARY_PATH=\
$VIRTUAL_ENV/lib/python3.12/site-packages/nvidia/cudnn/lib:\
$VIRTUAL_ENV/lib/python3.12/site-packages/nvidia/cublas/lib:\
$LD_LIBRARY_PATH
```

#### From flat schemas

```sh
quickbeam build \
  -s test.sond3r.track.invariants.3=0xc4103f... \
  -s test.sond3r.track.taxonomy.2=0x382fda... \
  --primary test.sond3r.track.invariants.3 \
  --graph-api-key <key> \
  --ipfs-gateway https://your-gateway.mypinata.cloud/ipfs \
  --dim 256 \
  --umap \
  --reset
```

#### From a schema bundle

```sh
quickbeam build \
  --bundle fangorn.mb.creativecore.v1=0xac92db425c174e4301cd41e81e16d99fd2c5f4e2f13b739004996e95875e990d \
  --root-type Track \
  --graph-api-key b66e8b18ae3fe2c5a91929098b290d69 \
  --ipfs-gateway https://green-reasonable-heron-957.mypinata.cloud/ipfs \
  --dim 256 \
  --umap \
  --reset 
```

`--bundle` and `--schema`/`--primary` are mutually exclusive. When `--bundle` is set the builder takes the edge-walk path.

#### Resuming a build

`quickbeam build` is fully resumable. Progress is saved to `--checkpoint-file` (default `./db/ingest_checkpoint.json`) at the granularity of individual bundle manifests. On re-run without `--reset`, already-completed manifests are skipped before any IPFS data is fetched — RAM usage stays flat regardless of how many records have already been embedded.

The checkpoint tracks two things:
- `completed_manifest_cids` — manifests that have been fully embedded (skipped on re-run).
- `processed_track_ids` — records within the currently in-flight manifest, used only for crash-recovery mid-manifest. Cleared when the manifest completes.

#### UMAP only (reproject existing collection)

```sh
quickbeam build --umap-only
```

### 3. Watch for new events (optional)

After an initial build, run `quickbeam watch` to keep the collection up to date as new manifests are published on-chain.

```sh
quickbeam watch \
  --bundle "fangorn.mb.bundle.v1=0xabc123..." \
  --graph-api-key <key> \
  --ipfs-gateway https://your-gateway.mypinata.cloud/ipfs \
  --ipfs-gateway-key <pinata-jwt> \
  --poll-interval 120
```

The watcher uses the same checkpoint file as the builder. On startup it reads `last_block` from the checkpoint and only queries subgraph events with `blockNumber_gt: last_block`, so it never re-scans the full history. The GPU model is loaded once and kept alive across poll cycles.

#### Filter hierarchy

All filters are optional and combinable. Narrower filters reduce both subgraph load and embedding work.

```sh
# Watch all publishers, all dataset names:
quickbeam watch --bundle fangorn.mb.bundle.v1=0xabc...

# Only a specific publisher:
quickbeam watch --bundle fangorn.mb.bundle.v1=0xabc... \
  --owner 0xdeadbeef

# Only certain dataset names (any publisher):
quickbeam watch --bundle fangorn.mb.bundle.v1=0xabc... \
  --dataset Track Recording

# Most specific — one publisher's named datasets:
quickbeam watch --bundle fangorn.mb.bundle.v1=0xabc... \
  --owner 0xdeadbeef --dataset Track
```

`--owner` and `--dataset` are both repeatable. `--dataset` filters on the `name` field of the `ManifestPublished` event — the name the publisher assigned when registering the dataset.

### 4. Start the server

```sh
quickbeam serve \
  -s test.sond3r.track.invariants.3=0xc4103f... \
  -s test.sond3r.track.taxonomy.2=0x382fda... \
  --primary test.sond3r.track.invariants.3 \
  --graph-api-key <key> \
  --ipfs-gateway https://your-gateway.mypinata.cloud/ipfs
```

The server starts immediately and serves whatever is already in Qdrant. Use `POST /reingest` to pull new subgraph data without restarting.

#### Serve + watch in one process

Pass `--watch` to run the live embedding daemon alongside the server, so one deployment both ingests and serves. **Everything before `--watch` configures the server; everything after it is forwarded verbatim to `quickbeam watch`.** The watcher writes to Qdrant; the server reads from it; the watcher is a child process that is terminated when the server exits.

```sh
quickbeam serve \
  --collection fangorn \
  --watch \
    --bundle "fangorn.mb.bundle.v1=0xabc123..." \
    --graph-api-key <key> \
    --ipfs-gateway https://your-gateway.mypinata.cloud/ipfs \
    --poll-interval 120
```

Note this loads the embedding model twice (once in each process), so plan VRAM accordingly — or run the two commands as separate services against the same Qdrant.

---

## Snapshots

A snapshot is a portable copy of the populated Qdrant collection. It lets you seed a new instance — including one without a GPU — from a pinned IPFS artifact.

``` sh  
curl -X POST localhost:6333/collections/fangorn/snapshots
# grab the latest snapshot from qdrant
docker exec qdrant-core find /qdrant -name "*.snapshot"
# exfiltrate the latest snapshot from docker and store locally
docker cp qdrant-core:/qdrant/snapshots/fangorn/fangorn-8009660693873684-2026-06-16-22-11-57.snapshot ~/.snapshot
# zip the snapshot
gzip -k ~/.snapshot
# pin to ipfs (from the root)
node src/pinata.mjs upload ~/.snapshot.gz "fangorn-8009660693873684-2026-06-16-22-11-57.snapshot.gz"
# note the sha256 sum of the snapshot before cleanup
sha256sum ~/.snapshot 
rm -rf ~/.snapshot ~/.snapshot.gz
```

### Export

```sh
# Full bundle (fields + embeddings — use this to seed a complete server)
quickbeam export --src http://localhost:8080 --out bundle.ndjson

# Embeddings only (track_id + vector — minimal artifact for vector-space clients)
quickbeam export --src http://localhost:8080 --out embeddings.ndjson --embeddings-only
```

### Pin to IPFS

```sh
gzip -k bundle.ndjson
node src/pinata.mjs upload bundle.ndjson.gz "quickbeam-bundle-v1"
```

See [Managing Pinata data](#managing-pinata-data) for listing and bulk-deleting pinned files.

### Export a Qdrant snapshot

```sh
# Write snapshot to Qdrant storage
curl -X POST localhost:6333/collections/fangorn/snapshots

# Find the file
docker exec qdrant-core find /qdrant -name "*.snapshot"

# Copy out and compress
docker cp qdrant-core:/qdrant/snapshots/fangorn/<snapshot-file> ~/.snapshot
gzip -k ~/.snapshot

# Pin to IPFS
node src/pinata.mjs upload ~/.snapshot.gz "<snapshot-file>.gz"

# Record the sha256 before cleanup
sha256sum ~/.snapshot
rm ~/.snapshot ~/.snapshot.gz
```

### Seed on startup

```sh
quickbeam serve \
  -s test.sond3r.track.invariants.3=0x... \
  --bundle-cid QmYourBundleCIDHere
```

If the collection is empty and `--bundle-cid` is provided, the server fetches the NDJSON from IPFS and upserts it in the background. The server is live immediately — results populate as the seed progresses. If the collection already has points, the seed is skipped.

### Manual import

```sh
# From a local file
cat bundle.ndjson | curl -X POST http://localhost:8080/bundle/import \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @-

# Stream directly between two instances
curl -N http://host-a:8080/bundle/export \
  | curl -X POST http://host-b:8080/bundle/import \
       -H "Content-Type: application/x-ndjson" \
       --data-binary @-
```

---

## Semantic CDN

The search server runs queries **server-side** — which means the node observes every
query vector, and a semantic query *is* intent. The Semantic CDN inverts this: the
operator distributes the **public** embeddings as static, content-addressed artifacts;
the user pulls a slice into their **own** local Qdrant and queries it offline. Knowledge
moves to the user, the network never sees a query. Full walkthrough in
[docs/SEMANTIC_CDN.md](docs/SEMANTIC_CDN.md); the short version:

```sh
# (operator) declare domains as filters over the collection
cat > domains.json <<'JSON'
{ "domains": {
  "music":  { "description": "Recordings & artists", "filter": { "entityType": ["Recording","Artist"] } },
  "venues": { "description": "Places & events",       "filter": { "entityType": ["Place","Event"] } }
} }
JSON

# (operator) bake immutable shards from Qdrant, then serve them statically
quickbeam cdn bake  --config domains.json --cdn-dir ./cdn --collection fangorn
quickbeam cdn serve --cdn-dir ./cdn --port 8090

# (user) pull a domain into a LOCAL collection, then query it offline
quickbeam pull music --cdn-url http://localhost:8090 --collection music_local
quickbeam serve --collection music_local      # local search — CDN sees nothing
```

A **domain** is operator-declared (a named `entityType`/`owner` filter, in `domains.json`).
`bake` writes `cdn/<domain>/shard-NNNN.ndjson.gz` (reusing the `/bundle/export` row shape)
plus a `manifest.json` carrying a **sha256 per shard**, and a top-level `catalog.json`.
`serve` is a separate minimal FastAPI app exposing only static reads (`/catalog`,
`/domains/{name}/manifest`, `/domains/{name}/shards/{file}`) with HTTP **Range** support,
so shards are cacheable and downloads resume. `pull` verifies every shard against its
sha256 and loads it into the local collection with deterministic point ids, so an
interrupted or repeated pull is safe.

---

## Managing Pinata data

`src/pinata.mjs` is a small CLI for the Pinata account that backs your IPFS pins (snapshots, bundles). It needs `PINATA_JWT` in the environment (or a `.env` at the repo root).

```sh
# Upload / pin a file (replaces the old pin.mjs)
node src/pinata.mjs upload ~/.snapshot.gz "sond3r.snapshot.2026-06-14.gz"

# List pins (optionally filter by name substring)
node src/pinata.mjs list
node src/pinata.mjs list --name sond3r

# Delete by file ID(s)
node src/pinata.mjs delete <id1> <id2>

# Bulk-delete every file whose name matches a substring (prompts unless --yes)
node src/pinata.mjs delete-pattern "sond3r.snapshot"

# Delete everything in the account (prompts unless --yes)
node src/pinata.mjs delete-all
```

Pinata's name filter is a **contains** match, not a strict prefix — name files with a consistent prefix (e.g. `sond3r.snapshot.*`) for clean targeting. Also available via `npm run pinata -- <args>`.

---

## Migrating to Qdrant Cloud

```sh
quickbeam migrate
```

`migrate.py` contains hardcoded source/destination credentials — edit it before running.

Then point the server at the cloud cluster:

```sh
quickbeam serve \
  -s ... \
  --qdrant-url https://your-cluster.cloud.qdrant.io:6334 \
  --qdrant-api-key <key>
```

---

## x402 payment gating

[x402](https://www.x402.org/) is the HTTP `402 Payment Required` protocol for paid APIs. quickbeam implements the `exact` scheme over an EVM stablecoin (USDC by default) using EIP-3009 `transferWithAuthorization` signatures. It lives in [`quickbeam/x402.py`](quickbeam/x402.py) and is used in two independent places:

1. **HTTP gating** — `quickbeam serve --x402-pay-to 0x...` installs middleware that gates the search routes (`/search`, `/search/vector`, `/search/text`).
2. **Per-tool gating** — `quickbeam mcp --x402-pay-to 0x...` charges the calling agent per MCP tool call (see [MCP server](#mcp-server)).

### The flow

1. Client calls a gated route with no `X-PAYMENT` header → server replies `402` with a JSON body `{ x402Version, accepts: [requirements], error }`.
2. Client signs an EIP-3009 authorization for the quoted price, base64-encodes the payment into `X-PAYMENT`, and retries.
3. Server verifies the signature, settles, and serves the response with an `X-PAYMENT-RESPONSE` header describing settlement.

Verification is pluggable. By default a **local verifier** recovers the EIP-712 signer and checks the authorization terms without broadcasting — suitable for testnets, demos, and tests. Point `--x402-facilitator <url>` at a real facilitator for on-chain verify + settle.

```sh
# Gate the HTTP search routes at 0.001 USDC per request on Base Sepolia:
quickbeam serve \
  -s test.sond3r.track.invariants.3=0x... \
  --x402-pay-to 0xYourReceivingAddress \
  --x402-price 0.001 \
  --x402-network base-sepolia
```

Supported networks: `base-sepolia` (default), `base`, `avalanche-fuji`. Each has a default USDC contract; override with `--x402-asset`.

### Agent-side helper

`quickbeam/x402.py` also ships `PayingClient`, an `httpx.AsyncClient` wrapper that transparently pays any `402` it receives (sign → retry → record settlement). This is the agent side, used by the test-suite and available for any Python client.

---

## MCP server

`quickbeam mcp` is a [Model Context Protocol](https://modelcontextprotocol.io/) server ([`quickbeam/mcp_server.py`](quickbeam/mcp_server.py)) that exposes the catalog to agents. It is a **thin, stateless HTTP client** of `quickbeam serve` — it holds no embedding model and no Qdrant connection; every tool delegates to the API and reshapes the response. Because the result shape is driven by the server's role map (`GET /schema`), the same MCP server works over a music corpus today and an OSM corpus tomorrow by changing only `--corpus` / `--domain`, never tool code.

```sh
# Phase 1 — free tools, remote streamable-http transport:
quickbeam mcp --transport http --host 0.0.0.0 --port 8765 \
  --api-url http://localhost:8080

# local stdio (MCP Inspector / Claude Desktop):
quickbeam mcp --transport stdio --api-url http://localhost:8080
```

### Tools

- **`semantic_search(query, limit=10)`** — meaning-based search. Embeds the query (server-side), returns records shaped from the role map: `{ id, title, subtitle, tags, score, provenance }`. The raw embedding vector is dropped (token bloat); provenance is attached to every hit.
- **`corpus_info()`** — the corpus domain, field roles, and record count, so an agent can decide relevance before searching.

### Provenance

Every result carries on-chain provenance as a first-class field, sourced from each Qdrant point's `meta`:

```json
"provenance": {
  "source_cid": "Qm…",          // manifest CID the record was published in
  "published":  "2026-06-14T…",  // ISO8601 from the block timestamp
  "version":    1,
  "publisher":  "0x…"            // publisher address
}
```

### Phase 2 — charge agents per call

x402 gating for the MCP is **phased and isolated** in [`quickbeam/mcp_payments.py`](quickbeam/mcp_payments.py); with no `--x402-pay-to`, none of it runs and the tools are free. When enabled, each gated tool gains an optional `payment` argument:

```sh
quickbeam mcp --transport http \
  --x402-pay-to 0xYourReceivingAddress \
  --x402-price 0.001 --x402-network base-sepolia
```

Since MCP has no HTTP headers, payment rides on a tool argument instead of `X-PAYMENT`:

1. Agent calls `semantic_search(query)` with no `payment` → the tool returns the x402 requirements: `{ payment_required: true, accepts: [...] }`.
2. Agent signs the quoted requirement and calls again with `payment=<base64>` → the tool returns results plus a `payment` settlement receipt.

The verify/settle primitives are reused verbatim from `x402.py`; only the transport (tool argument vs HTTP header) differs.

> **Embedding quality note.** nomic-embed-text-v1.5 is asymmetric — documents are embedded with a `search_document:` prefix and queries with `search_query:`. The `/search` route applies the query prefix automatically, so MCP results use the correct retrieval path. Existing indexed vectors are unaffected (they were correctly built as documents).

---

## Data pipelines

The `quickbeam data` subcommands generate seed data for testing. `quickbeam data fetch` outputs flat `{ name, fields }` JSONL consumed by the ingest server's flat-schema path. `quickbeam data mb` outputs v3 bundle chunk files (node chunks + edge chunk) consumed by `quickbeam build --bundle`.

### Last.fm + MusicBrainz

Scrapes artist discographies via the Last.fm API and optionally enriches with ISRC codes and contributors from MusicBrainz.

```sh
export LASTFM_API_KEY=your_key

quickbeam data fetch --volume 1 --max-gb 9.5
# Resumes automatically if interrupted — re-run the same command.
# When the volume ceiling is hit, upload and increment --volume.

quickbeam data fetch --volume 2 --max-gb 9.5
quickbeam data fetch --volume 1 --artists-file artists.txt   # custom seed list
quickbeam data fetch --volume 1 --no-mb                      # skip MusicBrainz lookups
```

Outputs `volume_<N>_core.jsonl` (structural) and `volume_<N>_taxonomy.jsonl` (genres/moods/themes/contexts).

### MusicBrainz JSON dump

Downloads the full MusicBrainz `release.tar.xz` dump (~23 GB) and extracts up to `--target-count` tracks with tag data. Resumable at every stage — re-run to pick up where it left off. The latest dump URL is discovered automatically; pass `--dump-url` to pin a specific one.

```sh
quickbeam data mb --volume 1 --target-count 50000 --output-dir ./data
quickbeam data mb --volume 1 --target-count 50000 --connections 8   # faster download
quickbeam data mb --help
```

The download uses `--connections` (default 4) parallel HTTP range requests, each writing to a non-overlapping slice of a pre-allocated file. A `.parts` sidecar tracks completed chunks so interrupted runs skip them on restart. Pass `--connections 1` to fall back to single-connection streaming.

Outputs three v3 bundle chunk files — ready to upload to IPFS and register as a bundle schema:

| File | Contents |
|---|---|
| `volume_<N>_tracks.json` | `[{ id, type: "Track", fields: { trackId, isrcCode, title, byArtist, albumName, datePublished, durationMs, contributors, _mbid } }, ...]` |
| `volume_<N>_taxonomies.json` | `[{ id: "taxonomy:<trackId>", type: "TrackTaxonomy", fields: { trackId, genres, moods, themes, contexts } }, ...]` |
| `volume_<N>_edges.json` | `[{ rel: "hasTaxonomy", from: "<trackId>", to: "taxonomy:<trackId>" }, ...]` |

These three files are the raw v3 bundle chunks — Track + TrackTaxonomy node files plus an edge file. Use `src/publish_mb_bundle.ts` (see **End-to-end workflow** below) to register schemas and publish them to Fangorn, then run `quickbeam build --bundle` to embed.

### OpenStreetMap changesets

Fetches recent changesets within a bounding box from the public OSM API. Demonstrates that adding a new domain is a schema change, not an architecture change — the same ingest server handles OSM data automatically via role inference (title←comment, subtitle←user_id, spatial←bbox, etc.).

```sh
# Edit BBOX, TARGET_COUNT, DAYS_BACK in quickbeam/pipelines/osm.py first, then:
quickbeam data osm
```

Outputs `stage_volumes/osm_changesets.json`.

---

## End-to-end workflow (MusicBrainz → Fangorn → Qdrant)

### Step 1 — Generate the bundle chunk files

```sh
quickbeam data mb --volume 1 --target-count 50000 --output-dir ./data
# produces: data/volume_1_tracks.json
#           data/volume_1_taxonomies.json
#           data/volume_1_edges.json
```

### Step 2 — Register schemas and publish to Fangorn

`src/publish_mb_bundle.ts` must be placed in the fangorn-sdk `src/` directory alongside `setup-embeddings-testdata.ts` (it imports `TestBed` and the SDK type system from there).

```sh
# from the fangorn-sdk root:
cp /path/to/quickbeam/embeddings/src/publish_mb_bundle.ts src/

pnpm dotenvx run -f .env -- tsx src/publish_mb_bundle.ts \
  --tracks      /path/to/data/volume_1_tracks.json \
  --taxonomies  /path/to/data/volume_1_taxonomies.json \
  --edges       /path/to/data/volume_1_edges.json \
  --dataset     ds.mb.v1
```

On first run this registers three schemas (all idempotent — safe to re-run):

| Schema | Name | Description |
|---|---|---|
| Track | `fangorn.mb.track.v1` | Invariant metadata per recording |
| TrackTaxonomy | `fangorn.mb.track.taxonomy.v1` | Genre / mood tags |
| Bundle | `fangorn.mb.bundle.v1` | Track `—hasTaxonomy→` TrackTaxonomy |

Large volumes are published in batches (`--batch-size`, default 2000). Progress is saved to `tmp/mb-publish-ledger.json` — re-run the same command to resume after a failure.

When done the script prints the bundle name and ID:

```
  bundle name : fangorn.mb.bundle.v1
  bundle id   : 0xabc123...
```

### Step 3 — Build embeddings

```sh
quickbeam build \
  --bundle "fangorn.mb.bundle.v1=0xabc123..." \
  --root-type Track \
  --graph-api-key <key> \
  --ipfs-gateway https://your-gateway.mypinata.cloud/ipfs \
  --ipfs-gateway-key <pinata-jwt> \
  --dim 256 \
  --umap \
  --reset
```

---

## Configuration reference

All config is via CLI flags. Run `quickbeam build --help` or `quickbeam serve --help` for the full list.

### `quickbeam build`

| Flag | Default | Description |
|---|---|---|
| `--schema` / `-s` | | `NAME=0x...` schema ID pair. Repeatable. Flat-schema join mode. |
| `--bundle` | | `NAME=0x...` bundle schema. Edge-walk join mode (replaces `--schema`/`--primary`). |
| `--primary` / `-p` | first schema | Schema whose entry names are the join key (flat-schema mode) |
| `--root-type` | `Track` | Bundle node type emitted as one record per node (bundle mode) |
| `--subgraph-url` | Fangorn studio URL | The Graph subgraph endpoint |
| `--graph-api-key` | `""` | The Graph gateway API key |
| `--ipfs-gateway` | `https://gateway.pinata.cloud/ipfs` | IPFS gateway |
| `--qdrant-host` | `localhost` | Qdrant host |
| `--qdrant-port` | `6333` | Qdrant HTTP port |
| `--qdrant-grpc-port` | `6334` | Qdrant gRPC port |
| `--collection` | `quickbeam` | Qdrant collection name |
| `--checkpoint-file` | `./db/ingest_checkpoint.json` | Resume state file |
| `--embedding-model` | `nomic-ai/nomic-embed-text-v1.5` | fastembed model name |
| `--dim` | `256` | Matryoshka output dimensions: 256, 512, or 768 |
| `--embed-batch` | `16` | GPU embed batch size — lower for small VRAM |
| `--searchable-fields` | `auto` | Comma-separated field allowlist, or `auto` |
| `--page-size` | `100` | Subgraph pagination page size |
| `--ipfs-timeout` | `20` | IPFS request timeout in seconds |
| `--concurrency` | `16` | Max concurrent IPFS fetches |
| `--umap` | `false` | Compute and store UMAP px/py after ingest |
| `--umap-only` | `false` | Skip ingest; only (re)compute UMAP on existing collection |
| `--umap-neighbors` | `15` | UMAP n_neighbors parameter |
| `--umap-min-dist` | `0.05` | UMAP min_dist parameter |
| `--reset` | `false` | Delete and recreate the Qdrant collection on startup |

### `quickbeam watch`

| Flag | Default | Description |
|---|---|---|
| `--bundle` | required | `NAME=0x...` bundle schema to watch |
| `--root-type` | `Track` | Bundle root node type |
| `--owner` | | Filter to this publisher address. Repeatable. |
| `--dataset` | | Filter to these dataset names. Accepts multiple values. |
| `--poll-interval` | `60` | Seconds between subgraph polls |
| `--subgraph-url` | Fangorn studio URL | The Graph subgraph endpoint |
| `--graph-api-key` | `""` | The Graph gateway API key |
| `--ipfs-gateway` | `https://gateway.pinata.cloud/ipfs` | IPFS gateway |
| `--ipfs-gateway-key` | | Bearer token for authenticated IPFS gateways |
| `--qdrant-host` | `localhost` | Qdrant host |
| `--qdrant-port` | `6333` | Qdrant HTTP port |
| `--qdrant-grpc-port` | `6334` | Qdrant gRPC port |
| `--collection` | `fangorn` | Qdrant collection name |
| `--checkpoint-file` | `./db/ingest_checkpoint.json` | Shared with `build` — tracks `last_block` and completed manifests |
| `--embedding-model` | `nomic-ai/nomic-embed-text-v1.5` | fastembed model name |
| `--dim` | `256` | Matryoshka output dimensions |
| `--embed-batch` | `16` | GPU embed batch size |
| `--role-map-file` | `./db/role_map.json` | Role map path — loaded if present, inferred on first cycle otherwise |
| `--searchable-fields` | `auto` | Field allowlist or `auto` |
| `--page-size` | `100` | Subgraph pagination page size |
| `--ipfs-timeout` | `20` | IPFS request timeout in seconds |
| `--concurrency` | `16` | Max concurrent IPFS fetches |

### `quickbeam serve`

| Flag | Default | Description |
|---|---|---|
| `--schema` / `-s` | | `NAME=0x...` schema ID pair. Repeatable. |
| `--primary` / `-p` | first schema | Join key schema |
| `--subgraph-url` | Fangorn studio URL | The Graph endpoint |
| `--graph-api-key` | `""` | The Graph gateway API key |
| `--ipfs-gateway` | `https://gateway.pinata.cloud/ipfs` | IPFS gateway |
| `--qdrant-url` | `None` | Qdrant Cloud URL — overrides `--qdrant-host`/`--qdrant-port` |
| `--qdrant-api-key` | `None` | Qdrant Cloud API key |
| `--qdrant-host` | `localhost` | Qdrant host (local) |
| `--qdrant-port` | `6333` | Qdrant HTTP port |
| `--qdrant-grpc-port` | `6334` | Qdrant gRPC port |
| `--collection` | `quickbeam` | Qdrant collection name |
| `--embedding-model` | `nomic-ai/nomic-embed-text-v1.5` | Must match the builder |
| `--bundle-cid` | `None` | IPFS CID of an NDJSON bundle to seed from on first startup |
| `--searchable-fields` | `auto` | Field allowlist or `auto` |
| `--host` | `0.0.0.0` | Bind host |
| `--port` | `8080` | Bind port |
| `--reset` | `false` | Drop and recreate collection on startup |
| `--x402-pay-to` | `None` | Recipient address. Enables x402 gating on the search routes when set. |
| `--x402-price` | `0.001` | Price per gated request in whole token units |
| `--x402-network` | `base-sepolia` | EVM network: `base-sepolia`, `base`, `avalanche-fuji` |
| `--x402-asset` | network USDC | Token contract address |
| `--x402-decimals` | `6` | Token decimals for the price → atomic conversion |
| `--x402-facilitator` | `None` | Facilitator URL for on-chain verify+settle (omit for local verification) |

Plus `--watch <watch args...>` to run the [live daemon alongside the server](#serve--watch-in-one-process).

### `quickbeam mcp`

| Flag | Default | Description |
|---|---|---|
| `--api-url` | `http://localhost:8080` | Base URL of the quickbeam HTTP API it delegates to |
| `--corpus` | `fangorn-music` | Corpus label returned with results |
| `--domain` | music description | One-line corpus domain — drives tool descriptions (the OSM-switch seam) |
| `--transport` | `http` | `http` (streamable-http), `stdio`, or `sse` |
| `--host` | `0.0.0.0` | Bind host (http/sse) |
| `--port` | `8765` | Bind port (http/sse) |
| `--x402-pay-to` | `None` | Recipient address. Enables per-tool payment (Phase 2) when set. |
| `--x402-price` | `0.001` | Price per tool call in whole token units |
| `--x402-network` | `base-sepolia` | EVM network |
| `--x402-asset` | network USDC | Token contract address |
| `--x402-decimals` | `6` | Token decimals |
| `--x402-facilitator` | `None` | Facilitator URL (omit for local verification) |

Env equivalents: `QUICKBEAM_API_URL`, `QUICKBEAM_CORPUS`, `QUICKBEAM_DOMAIN`.

### `quickbeam export`

| Flag | Default | Description |
|---|---|---|
| `--src` | required | Source server URL, e.g. `http://localhost:8080` |
| `--out` | `bundle.ndjson` | Output file path |
| `--owner` | `None` | Filter export to a single owner address |
| `--embeddings-only` | `false` | Export only `track_id` + `embedding`, omit fields and metadata |

### `quickbeam cdn bake`

| Flag | Default | Description |
|---|---|---|
| `--config` | `domains.json` | Operator domain config: `name → { description, filter }` |
| `--cdn-dir` | `./cdn` | Output directory for baked shards |
| `--collection` | `fangorn` | Source Qdrant collection to bake from |
| `--domain` | all | Bake only this one domain from the config |
| `--shard-size` | `50000` | Points per shard file |
| `--scroll-batch` | `2000` | Qdrant scroll page size |
| `--embedding-model` | `nomic-ai/nomic-embed-text-v1.5` | Recorded in the manifest (Qdrant doesn't store it) |
| `--qdrant-url` / `--qdrant-api-key` | `None` | Qdrant Cloud (overrides host/port) |
| `--qdrant-host` / `--qdrant-port` / `--qdrant-grpc-port` | `localhost`/`6333`/`6334` | Local Qdrant |

A domain's `filter` accepts `entityType: [...]` and `owner: [...]` (each a `MatchAny`);
multiple keys are AND-ed. An empty/missing filter selects the whole collection.

### `quickbeam cdn serve`

| Flag | Default | Description |
|---|---|---|
| `--cdn-dir` | `./cdn` | Directory of baked shards to serve |
| `--host` | `0.0.0.0` | Bind host |
| `--port` | `8090` | Bind port |
| `--cors` | `false` | Enable permissive CORS (for browser-based pulls) |

### `quickbeam pull`

| Flag | Default | Description |
|---|---|---|
| `domain` | required | Positional — domain name to pull (see the CDN's `/catalog`) |
| `--cdn-url` | `http://localhost:8090` | Base URL of the Semantic CDN |
| `--collection` | domain name | Local Qdrant collection to load into |
| `--cache-dir` | `./db/cdn_cache` | Where downloaded shards are cached |
| `--concurrency` | `4` | Parallel shard downloads |
| `--batch` | `500` | Upsert batch size |
| `--reset` | `false` | Recreate the local collection before loading |
| `--download-only` | `false` | Fetch + verify shards but don't load into Qdrant |
| `--qdrant-url` / `--qdrant-api-key` / `--qdrant-host` / `--qdrant-port` / `--qdrant-grpc-port` | local | Target Qdrant for the local collection |

---

## API

All endpoints return JSON. Hits are shaped as `{ id, fields, owner, meta, score?, embedding? }`, where `meta` carries on-chain provenance `{ manifestCid, blockTimestamp, version, owner }`.

> When `--x402-pay-to` is set, `/search`, `/search/vector`, and `/search/text` require an `X-PAYMENT` header — see [x402 payment gating](#x402-payment-gating).

### `GET /browse`
Paginated browse. `?limit=20&offset=0`

### `GET /search`
Semantic search by text — embeds the query server-side.
`?q=late+night+driving&n_results=10&owner=0x...`

### `POST /search/vector`
Query by raw embedding vector.
```json
{ "embedding": [...], "n_results": 20, "owner": "0x..." }
```

### `POST /search/text`
Lexical search over an in-memory index of title, subtitle, and tag fields. Faster than semantic search for exact name lookups.
```json
{ "q": "arctic monkeys", "limit": 20, "owner": "0x..." }
```

### `POST /embed`
Embed text using the same model as ingestion — keeps client and server embedding spaces aligned.
```json
{ "text": "late night melancholic indie" }   → { "embedding": [...] }
{ "texts": ["track one", "track two"] }       → { "embeddings": [[...], [...]] }
```

### `GET /schema`
Inferred semantic role map (`title`, `subtitle`, `tags`, etc.) and facet vocabularies for the active dataset.

### `GET /catalog/map`
2D UMAP projection of the full collection for a galaxy/map view. Computed on first request and cached. Returns `{ "computing": true }` while still running.

### `POST /catalog/map/refresh`
Invalidates the map cache and recomputes in the background.

### `GET /bundle/export`
Streams the full collection as NDJSON — one point per line. `?owner=0x...&limit=1000&offset=0`

### `POST /bundle/import`
Streaming NDJSON import — reads line by line, upserts in batches of 500.

### `POST /bundle/upsert`
JSON body upsert for pre-embedded points (smaller programmatic use).

### `GET /health`
Collection count, schema map, role map, cache state, checkpoint info.

### `POST /reingest`
Triggers a background re-ingestion from the subgraph. Only re-embeds changed documents.

### `POST /reingest/full`
Clears the checkpoint and re-ingests everything from scratch. Does not drop the Qdrant collection.

### `GET /debug`
Join diagnostics — matched/unmatched track IDs across primary and secondary schemas.

---

## Join semantics

### Flat schemas

- The `--primary` schema's entry names are the join key (`trackId`)
- All other schemas are indexed by entry name and merged into the primary field set; secondary wins on conflict (enrichment pattern)
- Multiple secondary entries per key are merged left-to-right; last writer wins within that schema
- Entries in secondary schemas with no matching primary entry are silently dropped

### Schema bundle

- One record is emitted per `--root-type` node; the root node's stable, publisher-assigned `id` is the join key
- Only outgoing edges from the root node are walked (one hop); each neighbor's fields are flattened in
- On key conflicts, the neighbor wins — same enrichment semantics as the flat secondary merge
- Edges into the root, and nodes not reachable from any root, contribute no fields
- Manifests that are not valid v3 bundles (missing `version: 3` or an `edgeChunk`) are skipped

In both modes the semantic role map (`title`, `subtitle`, `tags`, `spatial`, etc.) is inferred automatically from field names and value shapes across the merged dataset. This is what makes the same server and app work for music tracks, OSM changesets, or any other Fangorn schema without per-domain configuration.
