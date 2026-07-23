# quickbeam-alpaca — an example pluggable Source

A worked example of the quickbeam ingestion **harness**, sibling to
`example-robinhood-source`: an Alpaca Market-Data source that lives in its own
package and plugs into `quickbeam data …` with **no changes to quickbeam core**.

Where robinhood ingests raw EVM data from the Robinhood Chain, this ingests
daily bars + news from **Alpaca's Market Data v2 REST API**. A crawl is a
**(trading day, symbol universe)**; the default universe is Alpaca's most-actives
screener and the default day is the latest available session. Re-crawling a day
upserts (Assets are snapshots keyed on symbol, latest bar wins); the next day
advances the checkpoint. The goal is the same end-to-end pipeline: ingest →
publish → embed (quickbeam) → serve as an MCP.

| The source supplies | The harness owns |
|---|---|
| `read(cursor, args)` — Alpaca REST (bars + news + screener) | argparse (shared flags), the `--watch` poll loop |
| `build_graph(records)` — pure `events → {nodes}, [edges]` | `emit_volumes` → `volume_<n>_*.json` staging |
| `next_cursor(records, prev)` — max crawl day (YYYYMMDD) | checkpoint file, `--accumulate` ledger merge |
| `stems` / `snapshot_stems` / `role_map` / `presentation` | `--publish` → `fangorn repo init` + `commit` + `push` |

Two entity types: **Asset** (one daily-bar snapshot per symbol — OHLCV, change%,
VWAP) and **NewsItem** (recent headlines, linked `Asset --hasNews--> NewsItem`;
real prose, the richest thing to embed). `--symbols AAPL,MSFT,…` pins the
universe; `--day 2026-07-16` pins the day; `--no-news` drops news.

## Credentials

Free Alpaca API keys (the IEX feed is free): https://alpaca.markets → generate a
key pair, then:

```bash
export APCA_API_KEY_ID=...
export APCA_API_SECRET_KEY=...
# or pass --api-key / --api-secret
```

## Install

```bash
cd example-alpaca-source
pip install -e .          # or: pip install -e ".[dev]" to run the tests
```

`discover_sources()` reads the `quickbeam.sources` entry-point group, so once
installed the verb appears:

```bash
quickbeam data alpaca --help
```

## Use

```bash
export STAGE=./stage_volumes

# 1. INGEST — one crawl of the latest session for the top-100 most-active symbols.
quickbeam data alpaca --output-dir $STAGE --volume 1
#   --symbols AAPL,MSFT,NVDA pins the universe; --top N sizes the screener;
#   --day 2026-07-16 pins the day; --no-news drops news; --dry-run previews embed text.

# 2. PUBLISH — assemble the volumes into one {vertices,edges} batch and settle it into a
#    namespace (needs the `fangorn` CLI on PATH + a configured wallet).
quickbeam data alpaca --all-assets --output-dir $STAGE --volume 1 --publish --namespace alpaca 

# 3. LIVE LEDGER — periodically re-crawl, growing a superset ledger so the watcher never
#    tombstones prior news. Assets upsert each cycle; news accumulates.
quickbeam data alpaca --watch --poll-interval 3600 \
  --output-dir $STAGE --volume 1 --publish --namespace alpaca \
  --accumulate --checkpoint-file db/alpaca_ingest_day.json

# 4. EMBED + SERVE — read the namespace back, embed, ship CDN deltas, expose as an MCP.
export OWNER=0x147c24c5Ea2f1EE1ac42AD16820De23bBba45Ef6
# publisher wallet (fangorn repo init alpaca prints it)
quickbeam watch --source $OWNER:alpaca --collection alpaca --cdn-dir ./cdn --cdn-domain alpaca
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
quickbeam mcp --cdn-url http://localhost:8090 --transport http --port 8765
```

## Tests

```bash
pip install -e ".[dev]" && pytest        # pure build_graph / cursor, no network
```
