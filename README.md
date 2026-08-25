# quickbeam

Semantic search over [Fangorn](https://github.com/fangorn-network/fangorn) knowledge graphs. A publisher versions a graph offchain and anchors it onchain; quickbeam reads that graph with the `fangorn` light client, embeds it into Qdrant, and serves it via three means: an HTTP search API, a static "Semantic CDN" of downloadable shards, and an MCP server for agents.

---

## Install

```sh
python -m venv venv && source venv/bin/activate

pip install -e ".[gpu]"     # CUDA embeddings (fastembed-gpu)
pip install -e ".[cpu]"     # CPU-only
pip install -e ".[agent]"   # + MCP server and x402 payments
pip install -e ".[dev]"     # + pytest
```

Requires Python ≥3.12 and the [`fangorn` CLI](https://github.com/fangorn-network/fangorn) on `PATH` (or pass `--fangorn-bin`). The fangorn CLI refuses to start without
`ETH_PRIVATE_KEY`, but using a throwaway key is fine if you are not writing onchain. We recommend setting `PINATA_GATEWAY` too since the default `ipfs.io` is not guaranteed to serve content (ISP provider specific).

## Commands

| Command | What it does |
|---|---|
| `quickbeam build` | One shot: `fangorn read` one or more namespaces, project, embed into Qdrant |
| `quickbeam watch` | Daemon: `fangorn subscribe` and embed commits as they land (push-based) |
| `quickbeam serve` | The search API (`--watch` also runs the daemon in the same box) |
| `quickbeam cdn bake\|append\|edges\|precompute\|index\|serve` | Bake and serve static shards |
| `quickbeam pull` | Pull a CDN domain into a local Qdrant collection |
| `quickbeam mcp` | MCP server for agents. A local CDN pull-client |
| `quickbeam export` | Dump the collection as NDJSON |
| `quickbeam migrate` | Move a local collection to Qdrant Cloud |
| `quickbeam data …` | ETL pipelines and pluggable scraper sources |

Every command has its own `--help` flag for more info.

---

## Quickstart

### Run everything with compose

```sh
cp .env.example .env     # set PINATA_GATEWAY, ETH_PRIVATE_KEY, QDRANT_API_KEY, SOURCES_URL
docker compose up -d --build
```

Currently, there are four services contained in one image: `qdrant`, `watch`, `serve` (:8080), `cdn` (:8090), `mcp` (:8765). Namespaces are **not** configured in compose. The `watch` command polls the `SOURCES_URL` for its watch list and starts or cancels a stream per namespace without requiring a restart. Full deployment guide: **[`DOCKER-README.md`](DOCKER-README.md)**.

### Or run the pieces by hand

```sh
# 1. Qdrant
docker run -d -p 6333:6333 -p 6334:6334 -v "$(pwd)/db/qdrant:/qdrant/storage" qdrant/qdrant

# 2. Embed a namespace (one shot)
quickbeam build --source 0x147c24c5...:robinhood --root-profile asset

# 3. Serve it
quickbeam serve --port 8080
curl 'localhost:8080/search?q=semiconductors&n_results=5'
```

For CUDA, link the NVIDIA libs first (see [`gpu-env.sh`](gpu-env.sh)) and check with `python -c "import onnxruntime as ort; print(ort.get_available_providers())"`. Without CUDA it falls back to CPU automatically.

Live ingestion instead of a oneshot build:

```sh
quickbeam watch --app fangorn --source 0x147c...:robinhood --cdn-dir ./cdn
```

---

## Concepts

### A namespace is `app:publisher:subspace`

How the app portion is supplied depends on how sources are given.

**Static `--source` flags** — `build`, `watch` and `serve` take `OWNER:NAMESPACE` (repeatable, **two parts**). All of them run under the single `--app`, so one process covers one app. `*` on either side widens to the whole app:

```sh
--source 0x147c...:robinhood   # one publisher, one subspace
--source '0x147c...:*'         # one publisher, every subspace in the app
--source '*:docs'              # that subspace name across every publisher
--source '*:*'                 # the whole app
```

**A watch list — `watch --sources-url`** (what the compose deployment uses). Here **each entry carries its own app**, so one instance serves several. For example:

```json
["app1Id::", "app2Id::", "fangorn:0x147c...:robinhood"]
```
Here, we are watching all of app1, app2, but only a specific namespace for a specific publisher in the fangorn app.

Entries are `"APP:OWNER:NAMESPACE"` strings or `{app, owner, namespace}` objects, and an empty or `*` part is a wildcard. So `app1Id::` means *everything in app1*. `--app` (the `APP` env var in compose) is only the **fallback** for an entry naming no app. An entry with no app and no fallback is **dropped** because reading the wrong app silently indexes the wrong graph.

> Warning: **Don't pass the three-part form to `--source`.** It splits on the first colon only, so `--source fangorn:0xA:docs` silently becomes owner `fangorn`, namespace `0xA:docs` with no error and nothing watched.

> Note `--source` means something different on two other commands: `cdn edges --source` is a **path** to a linkset JSON, and `data events-fetch --source` names a **scraper** (`eventbrite` / `eventbrite-location` / `tribe`). Check the `--help` flag for the command you're running.

A wildcard source has no single namespace to seed with `fangorn read`, so it only sees namespaces published before it started if you also pass `--from-block N`.

### One collection, scoped by filter

Every watched namespace embeds into **one** Qdrant collection. Points carry `owner` at the payload top level and `meta.app` / `meta.namespace` nested, so a caller's slice is a filter instead of a copy of the vectors:

```
?scope=APP:OWNER:NAMESPACE     repeatable; triples OR, parts within a triple AND
?scope=:0xA:tracks             any part may be empty to leave it unconstrained
?scope=0xA:tracks              two-part OWNER:NAMESPACE still accepted
```

`scope` takes precedence over the separate `app` / `owner` / `namespace` params. `/browse` is not scoped and returns the whole collection.

### Root profiles

A profile walks the graph from every vertex carrying a given tag and folds its neighbors into one document. With no `--root-profile`, one profile is auto-derived per distinct vertex tag present in the source. Override with `--profiles-file` (see [`quickbeam/profiles.example.json`](quickbeam/profiles.example.json)):

```json
{ "file": { "root_type": "File", "max_depth": 2, "include": ["File"],
            "content_fields": ["filename", "text"] } }
```

`--max-depth`, `--label-cap` (max folded labels per relation group) and `--node-cap` (max nodes visited per root) bound the walk.

### Semantic roles

`quickbeam/roles.py` infers which fields are `title`, `subtitle`, `tags`, `temporal`, `spatial`, `media` from field names and value shapes so the same server and UI work over music, filings, or OSM changesets with no per-domain config. The inferred map is cached in `--role-map-file` and served at `GET /schema`.

---

## The search API (`quickbeam serve`)

JSON everywhere. Hits are `{ id, fields, owner, meta, score?, embedding? }`, where `meta` carries on-chain provenance.

| Route | |
|---|---|
| `GET /search?q=&n_results=&scope=` | Semantic search; embeds the query server-side |
| `POST /search/vector` | Query by raw vector — `{embedding, n_results, scope}` |
| `POST /search/text` | Lexical search over title/subtitle/tags |
| `POST /embed` | Embed text with the ingestion model — `{text}` or `{texts}` |
| `GET /browse?limit=&offset=` | Paginated browse of the whole collection |
| `GET /records?ids=a,b,c` | Fetch specific records by id (max 200), with vectors |
| `GET /adjacency?id=&rel=&dir=` | Relation groups, or the neighbor records. Needs `--adjacency-db`, else 501 |
| `GET /bucket/{n}?owner=` | Private retrieval — see below. 501 without `--index-layout`; empty without `cdn index --push-cells` |
| `GET /schema` | Inferred role map + facet vocabularies |
| `GET /catalog/map`, `POST /catalog/map/refresh` | 2-D UMAP projection of the collection |
| `GET /bundle/export?scope=` | Stream the collection as NDJSON |
| `POST /bundle/import`, `POST /bundle/upsert` | Load points back in |
| `POST /reingest`, `POST /reingest/full` | Re-read `--source` namespaces (changed only / everything) |
| `GET /health`, `GET /ready`, `GET /debug` | Counts, caches, checkpoint, join diagnostics |

Useful flags: `--collection`, `--qdrant-url` + `--qdrant-api-key` (remote Qdrant), `--catalog-map-file` (serve a prebuilt map instead of recomputing), `--dim` (default: read from the collection and Matryoshka-truncate queries to match).

`serve --watch` runs the daemon as a child process. Everything before `--watch` configures the server and everything after is forwarded to `quickbeam watch`.

```sh
quickbeam serve --collection fangorn --watch --source 0xA:docs --app fangorn
```

---

## Semantic CDN

Running queries server-side would mean the node sees every query vector. Because the embeddings are not opaque, vec2text-style inversion reconstructs short inputs almost exactly. The CDN inverts this paradigm. It bakes the collection into immutable shard files, serves them as static resumable downloads, and lets the client search locally.

```sh
# operator: declare domains as filters over the collection (domains.json), then bake
quickbeam cdn bake --config domains.json --cdn-dir ./cdn --collection fangorn
quickbeam cdn edges --cdn-dir ./cdn --domain mydomain --source linkset.json   # a FILE here
quickbeam cdn serve --cdn-dir ./cdn --cors --port 8090

# user: pull a domain into a LOCAL collection and query it offline
quickbeam pull mydomain --cdn-url http://localhost:8090 --collection mydomain
```

**Order matters: `bake` first, then `edges` / `precompute` / `index`.** The latter three write sidecars into the domain directory, and `bake` rmtrees and atomically replaces it. This means a re-bake discards everything installed before it.

A domain filter keys on `entityType`, `owner`, `namespace`, `app` (AND-ed; an empty filter selects everything which, on a shared collection, bakes *every* namespace into one domain, so always set at least `namespace`). The linkset `cdn edges` takes is a list of `{rel, from, to, fromType, toType}`, or `{"edges": [...]}` which is the shape `data linkgen` emits. `quickbeam watch --cdn-dir` bakes and appends live, naming each domain `{app8}-{owner8}-{namespace}`.

CDN routes: `GET /catalog`, `/events`, `/domains/{name}/manifest`, `/edges`, `/edges.gz`, `/shards/{file}`, `/index/{file}`, `/health`.

### Private retrieval (`cdn index` + `/bucket`)

For data collections that are too large to ship whole, `quickbeam cdn index` fits a **public** codebook over the vectors and buckets its cells. The client embeds locally, finds its nearest centroid, and asks the server for that centroid's *bucket*. The bucket is one integer and a deterministic public function of the query which is cacheable. It then re-ranks the returned candidates against its true vector. `--report` measures what the disclosure costs in recall.

Setup:

```sh
quickbeam cdn bake  --config domains.json --cdn-dir ./cdn --collection fangorn
quickbeam cdn index --cdn-dir ./cdn --domain mydomain \
                    --push-cells --collection fangorn   # writes the codebook AND backfills Qdrant
quickbeam cdn serve --cdn-dir ./cdn --cors --port 8090  # delivers codebook.i8 + layout.json
quickbeam serve --collection fangorn --port 8080 \
                --index-layout ./cdn/mydomain/index/layout.json
```

> Note: **`--push-cells` is required and queries will fail silently if it is not included.** `/bucket` filters Qdrant on a `cell` payload field that only `--push-cells` writes, so a server started with `--index-layout` against a collection that was never backfilled answers `200` with `{"count": 0, "results": []}`.

`cdn serve` and `serve` are separate processes on separate ports. The client pulls the public codebook from the first and sends its bucket id to the second. See [`examples/audius/audius-large-build/RUNBOOK.md`](examples/audius/audius-large-build/RUNBOOK.md) for a working four-process local setup over 1.9M records.

---

## MCP server (`quickbeam mcp`)

A self-contained **local pull-client** of the CDN. It downloads a domain's shards and searches them locally so the agent's query vector never leaves the machine.

```sh
quickbeam mcp --cdn-url http://localhost:8090 --transport http --host 0.0.0.0 --port 8765
quickbeam mcp --cdn-url http://localhost:8090 --transport stdio     # MCP Inspector / Claude Desktop
```

The endpoint is **`/mcp`**, not `/`.

Tools: `list_datasets`, `describe`, `search`, `get`, `neighbors`, `relations`, `aggregate`, `export`, `refresh`. There are two ways to navigate: semantic (`search`) and relational (`neighbors` walks typed linkset edges by node id). `aggregate` reduces in-process and returns an N-row table. `export` writes a column slice to a local file and returns only its path. Both exist so analytics over the entirety of the data don't stream every record through the model's context. Every result carries on-chain provenance.

---

## x402 payment gating

Set `--x402-pay-to` on `serve` or `mcp` and gated routes/tools return HTTP 402 until the caller supplies a valid `X-PAYMENT` header (x402 v1, `exact` scheme, EIP-3009 `transferWithAuthorization`).

```sh
quickbeam serve --x402-pay-to 0xRECV --x402-price 0.001 --x402-network base-sepolia
```

`--x402-asset` defaults to the network's USDC. `--x402-decimals` converts the price to atomic units. Without `--x402-facilitator` the server verifies signatures locally without broadcasting. Point it at a facilitator to verify and settle on-chain. `quickbeam/x402.py` is self-contained and also implements the agent side which signs and retries a 402.

---

## Publishing data

quickbeam is datasource agnostic, but requires developers to implement their own scraper. Implement the `Source` contract (`read` / `build_graph` / `next_cursor`, or subclass `SourceBase`) and hand it to `Publisher`:

```python
import quickbeam as qb
from my_scraper import MySource

qb.Publisher(MySource(), namespace="widgets").run()   # ingest → repo init → commit → push
```

A source package that registers itself under the `quickbeam.sources` entry-point group gets its own `quickbeam data <verb>` command along with the shared harness flags (`--watch`, `--publish`, `--dry-run`) without changes to the repo. Core registers none.

Built-in ETL pipelines under `quickbeam data`: `fetch` (Last.fm + MusicBrainz), `mb` / `mbpg` (MusicBrainz dump / Postgres), `places-fetch` (Google Places), `events-fetch` (Eventbrite / Tribe), `schemagen`, `linkgen`, `keylink`, `prebake`.

[`build_place.py`](build_place.py) wraps geocode → scrape → graph → embed → bake → demo into one interactive session; see [`docs/RUNNING_SCRIPT.md`](docs/RUNNING_SCRIPT.md).

---

## Examples

Each is a full publisher + app built on the Quickbeam toolchain:

- [`examples/audius`](examples/audius/audius-build/RUNBOOK.md): two sovereign publishers
  fused by a linkset, searched client-side in the browser
- [`examples/surgext`](examples/surgext/manual/README.md): the Surge XT manual as a
  searchable graph
- [`examples/sherwood`](examples/sherwood/example-robinhood-source/README.md): a
  `Source` implementation to copy
- [`examples/places`](examples/places/README.md): local discovery over Places + Events

---

## Deployment

Refer to [`DOCKER-README.md`](DOCKER-README.md) for the full guide.

```sh
./deploy.sh [--env] [--fresh] [--dry-run]      # build, push to Artifact Registry, pull on the box
./deploy-sources.sh [path/to/sources.json]     # deploy with a STATIC watch list instead
```

`--fresh` drops the qdrant and data volumes and re-embeds everything from chain.

## Run Tests

```sh
pytest tests -q
```

## Layout

```
quickbeam/
  cli.py            typer entry point; every command passes through to its own argparse
  ingest/           the shared ingestion engine (build + watch)
    build.py          `quickbeam build`
    embed.py          fastembed engine (GPU-OOM resilient), doc text, Qdrant indexes
    identity.py       deterministic point ids + the matryoshka transform
    checkpoint.py     resumable state
    umap.py           2-D projection → catalog map
    graph/projection.py  root profiles, the graph walk, join helpers
    sources/fangorn.py   `fangorn read` / `fangorn subscribe` bridge
    scrapers/         the Source contract + the ingestion harness
  watcher.py        `quickbeam watch`
  server.py         `quickbeam serve` (FastAPI)
  cdn.py            `quickbeam cdn *` — bake / append / edges / precompute / index / serve
  index.py          codebook, quantization, the privacy/recall harness
  pull.py           `quickbeam pull`
  mcp_server.py     `quickbeam mcp`
  x402.py           HTTP 402 payments, both sides
  roles.py          schema-agnostic semantic role inference
  objects.py        the git-native object model (commit/tree/blob), Python side
  publish.py        the `Publisher` façade
  pipelines/        built-in ETL sources
```

## Gotchas

- **`--poll-interval` is a reconnect backoff, not a monitoring interval.** `watch` is push-based off `fangorn subscribe`. The watch-list refresh is `--sources-refresh`.
- **`--app` must match whatever published the data.** A mismatched app resolves an empty namespace with no error anywhere.
- **`PINATA_GATEWAY` is a bare host**. The SDK appends `/ipfs/<cid>` itself.
- **`ETH_PRIVATE_KEY` is required even for read-only use.** Don't bake a `~/.fangorn/config.json` into an image since it will short circuit every env var.
- **A domain with no `namespace` filter bakes every namespace in the collection.**
- The MCP endpoint is `/mcp`.
