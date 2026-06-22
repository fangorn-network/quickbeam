# Common Crawl $\times$ Fangorn

## on-demand data APIs

This document describes the end-to-end flow that turns a request like *"build a
data API about hot wheels"* into a live, searchable dataset by:
1.  scraping [Common Crawl](https://commoncrawl.org) on demand 
2.  with [CmonCrawl](https://github.com/hynky1999/CmonCrawl)
3.  and publishing the results to [Fangorn](https://github.com/fangorn-network)
4.  then serving them through the existing quickbeam embedding + search stack.

The guiding principle (the same one the OSM/MusicBrainz pipelines already prove):
**adding a data source is a registration change, not an architecture change.**

---

## 1. The big picture

```
┌──────────────────────┐   publish crawl_job manifest     ┌──────────────────────────┐
│  Agentic UI / user   │  (routes + extractor + query     │   Fangorn (Arbitrum      │
│  "data API about X"  │   + embedded x402 payment)       │   Sepolia + IPFS)        │
│  LLM writes extractor├───────────────────────────────>  │   SchemaRegistry         │
└──────────────────────┘                                  │   DataSourceRegistry     │
        ▲                                                 └───────────┬──────────────┘
        │ search results                                              │ ManifestPublished
        │                                                             ▼
┌───────┴───────────┐    embed + serve     ┌───────────────────────────────────────┐
│  serve / mcp      │<─────────────────────│  quickbeam scrape (scraper service)   │
│  (x402 on data)   │   watch the output   │  1. verify embedded payment           │
└───────────────────┘   schema             │  2. cmon download (Common Crawl)      │
        ▲                                  │  3. cmon extract  (SANDBOXED)         │
        │  quickbeam watch                 │  4. publish records → Fangorn         │
        └──────────────────────────────────┴───────────────────────────────────────┘
```

### Components

| Component             | Command                           | Role                                                                                               |
| --------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------- |
| **Scraper service**   | `quickbeam scrape`                | Reacts to `crawl_job` manifests, crawls Common Crawl, publishes extracted records back to Fangorn. |
| **Embedding builder** | `quickbeam watch` + `serve`/`mcp` | Watches the *output* schema, embeds new records into Qdrant, serves search.                        |

The crawl pipeline core (`quickbeam/crawl/`) is shared and reusable: the scraper
service uses it for on-chain jobs, and `quickbeam data crawl` uses it for offline
development.

---

## 2. Roles & trust

| Actor                                               | Holds                                                 | Does                                                                               |
| --------------------------------------------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **Job publisher** (UI / user, on the user's behalf) | a wallet, `PINATA_JWT` to publish                     | generates the extractor, signs the payment, publishes the `crawl_job` manifest     |
| **Scraper operator**                                | `FANGORN_PRIVATE_KEY`, `PINATA_JWT`, a CmonCrawl venv | runs `quickbeam scrape`; gets paid per job; publishes the output dataset           |
| **Data consumer**                                   | a wallet                                              | queries the produced dataset via `serve`/`mcp` (pays the *data owner*, separately) |

Extractor Python is **LLM-generated but treated as untrusted**: a `crawl_job`
manifest can be published by any address, and it runs over untrusted Common Crawl
HTML. It is therefore always sandboxed (see [§6](#6-security)).

---

## 3. The `crawl_job` schema

`crawl_job` is a **generic, reusable Fangorn resolver schema**, authored in
Fangorn's `@type` grammar so every published job is validated at publish time. The
canonical definition lives in [`schemas/crawl_job.json`](schemas/crawl_job.json)
(`{ definition, types }`) and is registered like any other schema:

```sh
node src/publish.mjs --schema fangorn.crawljob.v1 \
  --schema-def schemas/crawl_job.json --register-only
```

### Shape

Top-level fields (with their custom `types`):

| Field             | `@type`                          | Notes                                                                                               |
| ----------------- | -------------------------------- | --------------------------------------------------------------------------------------------------- |
| `routes`          | `array<route>`                   | CmonCrawl extract routes, verbatim                                                                  |
| `extractors`      | `array<extractorModule>` \| null | extractor code by inline `source` and/or IPFS `sourceCid`                                           |
| `query`           | `crawlQuery`                     | what to fetch; `limit` caps the crawl (and the price)                                               |
| `outputSchema`    | `string`                         | schema name the produced records publish under                                                      |
| `outputSchemaDef` | `object` \| null                 | optional freeform resolver definition to register for the output                                    |
| `paymentReceipt`  | `string` \| null                 | base64 x402 ERC-3009 authorization (see [§5](#5-payment--pay-for-compute-embedded-in-the-manifest)) |

Custom types: `route { regexes: array<string>, extractors: array<routeExtractor> }`,
`routeExtractor { name, since|null, to|null }`,
`extractorModule { name, language|null, source|null, sourceCid|null }`,
`crawlQuery { urls: array<string>, matchType(enum), since|null, to|null, limit(range≥1), aggregator(enum)|null, filterNon200|null }`.

### Example record

```jsonc
{
  "routes": [
    { "regexes": [".*"],
      "extractors": [{ "name": "hotwheels", "since": "2024-01-01", "to": "2024-06-01" }] }
  ],
  "extractors": [                               // typed array (canonical on-chain form)
    { "name": "hotwheels", "language": "python",
      "source": "from cmoncrawl.processor.pipeline.extractor import BaseExtractor\n...\nextractor = HotWheels()\n",
      "sourceCid": null }
  ],
  "query": {
    "urls": ["hotwheels.fandom.com"], "matchType": "domain",
    "since": "2024-01-01", "to": "2024-06-01", "limit": 100, "aggregator": "gateway"
  },
  "outputSchema": "fangorn.webpage.v1",
  "outputSchemaDef": { "title": { "@type": "string" }, "url": { "@type": "string" } },
  "paymentReceipt": "<base64 x402 authorization>"
}
```

Notes:
- `extractors[].name` must match a `routes[].extractors[].name` **and** the module
  must expose a top-level `extractor` variable — CmonCrawl's contract.
- Each `extractorModule` carries inline `source` (wins) and/or an IPFS `sourceCid`
  (resolved at run time). `quickbeam/crawl/config.py` also accepts a `{name: source}`
  map as an offline convenience.
- The **output schema** (`outputSchema`) is itself a generic Fangorn schema —
  register it separately (e.g. [`schemas/webpage.json`](schemas/webpage.json))
  or inline it as `outputSchemaDef` to have the scraper register it idempotently.

---

## 4. End-to-end sequence

```
Job publisher (UI/agent)                Fangorn / subgraph            Scraper operator (quickbeam scrape)
────────────────────────                ──────────────────            ───────────────────────────────────
1. LLM writes extractor.py + routes
2. GET /pricing  ───────────────────────────────────────────────────▶ returns {payTo, base, perUnit, ...}
3. price = base + perUnit*limit
4. sign x402 authorization (ERC-3009)
   for `price` → payTo
5. publish crawl_job manifest ────────▶ ManifestPublished event
   (paymentReceipt = signed auth)        │
                                         └─ subgraph indexes it ─────▶ 6. poll detects new manifest
                                                                       7. resolve job fields from IPFS
                                                                       8. recompute price from query.limit
                                                                       9. verify + settle paymentReceipt
                                                                      10. cmon download (Common Crawl HTML)
                                                                      11. cmon extract  (SANDBOXED)
                                                                      12. transform → {name, fields}
                                                                      13. publish → outputSchema  ──────────┐
                                                                                                            │
quickbeam watch --bundle outputSchema=0x… ◀─────────────────────────── ManifestPublished (output) ◀────────┘
   → embeds into Qdrant
quickbeam serve / mcp  → search the new "hot wheels" data API
```

---

## 5. Payment — pay for *compute*, embedded in the manifest

The trigger is on-chain, so payment travels **inside the publish event**, not over
a separate HTTP call.

- **Quote.** `GET /pricing` (free) advertises `payTo`, `network`, `asset`, `base`,
  `perUnit`. `POST /pricing/quote` returns the exact atomic price + full
  `PaymentRequirements` for a specific job.
- **Pricing.** `price = base + per_unit × query.limit` (atomic units). Deterministic
  and known *before* signing, because `limit` caps the crawl.
- **Authorization.** The client signs **one** x402
  [ERC-3009 `transferWithAuthorization`](https://eips.ethereum.org/EIPS/eip-3009)
  for `price` → `payTo` and embeds it in `crawl_job.paymentReceipt`. It is a
  single-use *bearer* authorization (nonce-protected, fixed amount, fixed
  recipient), so publishing it on-chain is safe.
- **Verification.** On the matching event the listener recomputes the price from the
  job's own `query.limit`, then **verifies + settles** the authorization via
  `quickbeam/x402.py` before doing any work:
  - testnet: `LocalVerifier` (signature + terms, no broadcast)
  - mainnet: `--x402-facilitator <url>` → on-chain verify + settle
  - Underpay / wrong recipient / expired → **rejected**; manifest-CID dedupe
    prevents double-settle.
- **Dev bypass.** `--no-require-payment` runs jobs without an authorization.

### Compute vs. data access — two different payments

| Payment                         | Payer → Payee                    | Mechanism                                                                                  | Where         |
| ------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------ | ------------- |
| **Crawl compute** (run the job) | job publisher → scraper operator | x402 authorization embedded in the `crawl_job` manifest                                    | **this gate** |
| **Data access** (query results) | data consumer → dataset owner    | Fangorn `SettlementRegistry` (price-root + Semaphore privacy) and/or x402 on `serve`/`mcp` | downstream    |

Both move money over the same ERC-3009 rail; they are different payer→payee
relationships, not competing mechanisms. Fangorn's `SettlementRegistry` is built for
*selling access to already-published data* (it requires a publisher-committed price
root, pays the data owner, and adds anonymity) — so it belongs to the data-access
layer, **not** the compute gate.

---

## 6. Security

The extract step runs publisher-supplied Python over untrusted HTML, so
`quickbeam/crawl/sandbox.py` confines it:

- wall-clock timeout + `RLIMIT_CPU` / `RLIMIT_AS` (memory) / `RLIMIT_FSIZE`
- a scrubbed environment — **no secrets** (`FANGORN_PRIVATE_KEY`, `PINATA_JWT`, …)
  ever reach the extractor
- a fresh process session (`setsid`)
- network: the crawl runs in CmonCrawl's **`record` mode**, which fetches each
  capture's WARC content *during* extract (CmonCrawl's trusted downloader, in the
  same process as the extractor), so the extract step needs network and is **not**
  run in a network namespace. The only thing reachable over that network is public
  Common Crawl data, and the env carries no secrets. (`html` mode — content already
  on disk — *can* be fully net-isolated via `unshare --net`, but it routes every
  file by a single URL and is unsuitable for multi-page crawls.)

This is the MVP. The production target is a disposable container / gVisor /
Firecracker microVM per job **with egress filtered to Common Crawl endpoints**;
`quickbeam/crawl/sandbox.py:run` is the single seam to swap. CmonCrawl also runs
**out-of-process** via its CLI (not imported), which keeps its old-pydantic
dependency and the untrusted code out of the service interpreter.

---

## 7. Code map

| Path                               | Responsibility                                                             |
| ---------------------------------- | -------------------------------------------------------------------------- |
| `quickbeam/crawl/config.py`        | `CrawlJob` / `CrawlQuery` parsing; `payment_object()`; price key           |
| `quickbeam/crawl/cmon.py`          | wrappers around the upstream `cmon` CLI (`download`, `extract`)            |
| `quickbeam/crawl/sandbox.py`       | confined execution of the extract step                                     |
| `quickbeam/crawl/materialize.py`   | write extractor modules + `config.json` for `cmon extract`                 |
| `quickbeam/crawl/transform.py`     | extract output → `{name, fields}` (stable, de-duped names)                 |
| `quickbeam/crawl/pipeline.py`      | `run_crawl()` orchestration (materialize → download → extract → transform) |
| `quickbeam/scraper_service.py`     | subgraph listener, payment verify/settle, job runner, FastAPI endpoints    |
| `quickbeam/fangorn_publish.py`     | Python → `node src/publish.mjs` publish bridge                             |
| `src/publish.mjs`                  | `@fangorn-network/sdk`: idempotent schema register + `publishRecords`      |
| `quickbeam/pipelines/cmoncrawl.py` | `quickbeam data crawl` — offline one-shot                                  |
| `schemas/crawl_job.json`           | generic `@type` schema for crawl jobs (validated at publish)               |
| `schemas/webpage.json`             | example generic output schema for crawled pages                            |

Reused unchanged: `quickbeam/x402.py` (payment), `quickbeam/roles.py` (role
inference), the `watch`/`serve`/`mcp` stack, and the subgraph/IPFS access patterns
from `quickbeam/server.py` + `quickbeam/watcher.py`.

The Fangorn SDK default config (`FangornConfig.ArbitrumSepolia`) already carries the
live registry addresses:

| Registry           | Address                                      |
| ------------------ | -------------------------------------------- |
| SchemaRegistry     | `0xecafc21ca3ec41c020287fb8c2126b1a9af9d220` |
| DataSourceRegistry | `0x207ab1866704b2adc34e8ec1069fb8febafff2fd` |
| SettlementRegistry | `0x93a5e93e76a3c150d35d4cd40029e4f45f3e650f` |

---

## 8. Setup

CmonCrawl pins an old pydantic that conflicts with fastapi/mcp — install it in its
**own venv** and point the service at that binary. Publishing to Fangorn uses
the Fangorn SDK `@fangorn-network/sdk`.

```sh
# 1. CmonCrawl in an isolated venv
python -m venv /opt/cmon-venv && /opt/cmon-venv/bin/pip install cmoncrawl

# 2. install the fangorn sdk
npm i -g @fangorn-network/sdk
fangorn init
# then follow the prompts

# 3. Env the publisher needs
export FANGORN_PRIVATE_KEY=0x…              # signer for register/publish txs (Arbitrum Sepolia)
export PINATA_JWT=…                         # IPFS pinning
export PINATA_GATEWAY=your-gw.mypinata.cloud   # optional
export GRAPH_API_KEY=…                      # subgraph (optional)

# 4. Register the schemas with the Fangorn SDK
fangorn schema register fangorn.crawl.job.v0
fangorn schema register fangorn.webpage.v0
```

### Run the scraper service

```sh
quickbeam scrape \
  --crawl-job-schema fangorn.crawljob.v1=0x<schemaId> \
  --cmon-bin /opt/cmon-venv/bin/cmon \
  --graph-api-key "$GRAPH_API_KEY" \
  --x402-pay-to 0x<operator> --x402-price-base 0.05 --x402-price-per-unit 0.001 \
  --x402-network base-sepolia \
  --poll-interval 60 --port 8090
```

### Embed + serve the produced dataset (existing stack)

```sh
quickbeam watch --bundle fangorn.webpage.v1=0x<schemaId>
quickbeam serve --x402-pay-to 0x<dataOwner>      # gate data access (separate payment)
```

---

## 9. Develop an extractor offline first

`quickbeam data crawl` runs the **same pipeline** locally — no chain, no payment, no
SDK — so you can iterate before wiring the on-chain path:

```sh
quickbeam data crawl \
  --routes ./routes.json --extractors ./my_extractors \
  --url hotwheels.fandom.com --match-type domain \
  --since 2024-01-01 --to 2024-06-01 --limit 50 \
  --cmon-bin /opt/cmon-venv/bin/cmon \
  --out ./stage_volumes/crawl.json
```

`routes.json` is the CmonCrawl routes array; `./my_extractors` is a directory of
`<name>.py` modules referenced by the routes.

### Publish records to Fangorn directly

`src/publish.mjs` is a standalone bridge (also used by the service):

```sh
node src/publish.mjs --records recs.jsonl --schema fangorn.webpage.v1 \
  --dataset ds.hotwheels.2026 --schema-def schema.json
```

---

## 10. Service API

| Endpoint              | Purpose                                                                    |
| --------------------- | -------------------------------------------------------------------------- |
| `GET /health`         | liveness + last processed block                                            |
| `GET /pricing`        | free: `payTo`, `network`, `asset`, `base`, `perUnit`, formula — how to pay |
| `POST /pricing/quote` | exact atomic price + `PaymentRequirements` for a specific job              |
| `GET /jobs/{id}`      | per-job status (`running` / `published` / `failed`, manifest URI, payer)   |

---

## 11. Verification

- **Offline unit tests** (`tests/test_crawl.py`, `tests/test_scraper_payment.py`):
  job parsing, price scaling, materialize (incl. path-traversal rejection),
  transform, sandbox, full `run_crawl` with download/extract injected, and payment
  verification (valid / missing / underpaid / wrong-recipient / dev-bypass). Run:
  ```sh
  python -m pytest tests/ -q
  ```
- **Offline pipeline**: `quickbeam data crawl …` over a tiny crawl slice → inspect
  the emitted `{name, fields}` JSON.
- **End-to-end (testnet)**: register the `crawl_job` + output schemas; publish a job
  manifest with a signed `paymentReceipt`; run `quickbeam scrape`; confirm it
  crawls, publishes an output manifest, then `quickbeam watch` embeds it and
  `serve`/`mcp` search returns the new records.

---

## 12. Out of scope / future work

- **Agentic chat UI** — the LLM that writes extractors and orchestrates
  publish + pay. The service contract (`crawl_job` schema, `GET /pricing`,
  embedded x402) is the seam it targets.
- **Real settlement** — `LocalVerifier` does not broadcast; point
  `--x402-facilitator` at a facilitator (or settle on-chain) to actually collect.
- **Metered pricing** — per-record billing after the crawl would need an
  escrow/deposit model; current pricing is `base + per_unit × limit`, known up front.
- **Production sandbox** — swap the subprocess sandbox for a per-job
  container/microVM.
- **Selling the produced dataset** via Fangorn's `SettlementRegistry`
  (price-root + Semaphore-private settlement).
