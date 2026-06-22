# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`quickbeam` — infrastructure for building and serving vector search over on-chain data sources registered with [Fangorn](https://github.com/fangorn-network/fangorn). The pipeline pulls manifests from The Graph (subgraph), resolves payloads from IPFS, joins across schemas, embeds via fastembed/ONNX, and stores in Qdrant. A separate scraper service scrapes Common Crawl on-demand in response to on-chain `crawl_job` manifests.

The guiding design principle, repeated throughout: **adding a data source is a registration change, not an architecture change.** Schema-agnostic role inference (`quickbeam/roles.py`) is what lets the same server/app work for music tracks, OSM changesets, or scraped webpages without per-domain code.

## Commands

```sh
# Setup (Python venv + editable install)
python -m venv venv && source venv/bin/activate
pip install -e ".[gpu]"          # CUDA embeddings (build); or .[cpu] for CPU-only
pip install -e ".[dev]"          # pytest + fastmcp + eth-account (to run tests)

# Tests
python -m pytest tests/ -q       # full suite
python -m pytest tests/test_crawl.py -q
python -m pytest tests/test_x402_agent.py::test_name -q   # single test

# Node side (IPFS pinning + Fangorn publish bridge)
npm install
node src/pinata.mjs <upload|list|delete|delete-pattern|delete-all> ...   # needs PINATA_JWT
node src/publish.mjs --records recs.jsonl --schema NAME --dataset DS      # needs FANGORN_PRIVATE_KEY, PINATA_JWT
```

There is no lint step and no JS test runner configured (`npm test` is a stub). The README is the authoritative usage reference — consult it for the full flag matrices and end-to-end workflows.

## CLI architecture — important

`quickbeam/cli.py` is a thin Typer shell. Each subcommand uses **passthrough** (`allow_extra_args`, `ignore_unknown_options`, `add_help_option=False`): it rewrites `sys.argv` via `_fwd()` and then calls the `main()` of the real module, which parses args with its **own `argparse` parser**.

Consequences when editing:
- To add/change a flag for `build`, edit the `argparse` parser in `quickbeam/embeddings.py` — **not** `cli.py`. Same for `serve` → `server.py`, `watch` → `watcher.py`, `mcp` → `mcp_server.py`, `scrape` → `scraper_service.py`, `export` → `export_bundle.py`, and `data *` → `quickbeam/pipelines/*`.
- `quickbeam serve --watch ...` splits args at `--watch`: everything before configures the server, everything after is forwarded verbatim to a child `quickbeam watch` subprocess (terminated when serve exits). The watcher writes to Qdrant; the server reads from it.

## Core modules

| Module | Role |
|---|---|
| `quickbeam/embeddings.py` | `build` — offline ingest. Subgraph → IPFS → join → embed → Qdrant. Resumable via checkpoint file at per-manifest granularity. |
| `quickbeam/watcher.py` | `watch` — live daemon. Polls subgraph with `blockNumber_gt: last_block`, keeps the GPU model loaded across cycles. Shares the checkpoint file with `build`. |
| `quickbeam/server.py` | `serve` — FastAPI read-only search API (largest module, ~1700 lines). Does not ingest on startup; can seed from an IPFS NDJSON bundle (`--bundle-cid`). |
| `quickbeam/mcp_server.py` | `mcp` — stateless MCP server; a thin HTTP client of `serve` that reshapes results via the role map and attaches on-chain provenance. Holds no model/Qdrant connection. |
| `quickbeam/roles.py` | Schema-agnostic semantic role inference (`title`/`subtitle`/`tags`/`spatial`/etc.) from field names + value shapes. The keystone of domain-independence. |
| `quickbeam/x402.py` | x402 (`402 Payment Required`) over EVM stablecoin via EIP-3009. Used by both HTTP gating and per-tool MCP gating. `LocalVerifier` (no broadcast) for testnet; `--x402-facilitator` for mainnet. Also ships `PayingClient` (agent side). |
| `quickbeam/mcp_payments.py` | Phased/isolated x402 gating for MCP tools — inert unless `--x402-pay-to` is set. |
| `quickbeam/scraper_service.py` | `scrape` — subgraph listener + payment verify/settle + job runner + FastAPI endpoints. |
| `quickbeam/crawl/` | Shared Common Crawl pipeline (config/cmon/sandbox/materialize/transform/pipeline). Used by both `scrape` and the offline `data crawl`. |
| `quickbeam/pipelines/` | Seed-data generators: `lastfm.py`, `mb.py` (MusicBrainz), `osm.py`, `cmoncrawl.py`. |
| `quickbeam/fangorn_publish.py` | Python → `node src/publish.mjs` publish bridge. |

## Record shape & join modes

The builder produces one record shape — `{ track_id, fields, meta }` — via two interchangeable join phases:
- **Flat schemas** (`--schema`/`--primary`): schemas fetched independently, deduped (newest manifest wins), joined on the primary schema's entry name.
- **Schema bundle** (`--bundle`): a single bundle manifest carries typed node chunks + an edge chunk (v3 format); the builder walks outgoing edges from each `--root-type` node and flattens neighbor fields in.

`--bundle` and `--schema`/`--primary` are mutually exclusive. Everything downstream (role inference, embedding text, Qdrant payload) is identical for both modes.

## Embedding model note

Default model `nomic-ai/nomic-embed-text-v1.5` is **asymmetric**: documents are embedded with a `search_document:` prefix, queries with `search_query:`. The `/search` route applies the query prefix automatically. A `serve` instance's `--embedding-model` and `--dim` must match what the builder used.

## Scraper / Common Crawl constraints

CmonCrawl pins an old pydantic that conflicts with fastapi/mcp, so it is **not** a dependency here. Install the `cmon` CLI in its own venv and point the service at it via `--cmon-bin` / `$CMON_BIN`. The extract step runs publisher-supplied, untrusted Python over Common Crawl HTML and is therefore **always sandboxed** (`quickbeam/crawl/sandbox.py`: rlimits, scrubbed env with no secrets, fresh session). `sandbox.py:run` is the single seam to swap for a production container/microVM.

Payment for scraping rides **inside the manifest** (`crawl_job.paymentReceipt`, a signed single-use ERC-3009 bearer authorization), not over HTTP, because the trigger is on-chain. This is distinct from the data-access payment (x402 on `serve`/`mcp`, or Fangorn's `SettlementRegistry`) — different payer→payee, same rail. See `CC_Fangorn.md` for the full design.

## Environment variables

`FANGORN_PRIVATE_KEY` (publish/register txs, Arbitrum Sepolia), `PINATA_JWT` (IPFS pinning, also read from `.env`), `PINATA_GATEWAY`, `GRAPH_API_KEY`, `LASTFM_API_KEY` (data fetch), `CMON_BIN`. Fangorn registry addresses live in the SDK's `FangornConfig.ArbitrumSepolia` (see `CC_Fangorn.md` §7).
