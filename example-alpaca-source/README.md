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

## Price history — the backtest feed (`--price-history`)

The Robinhood-Chain tokens mirror real equities 1:1, so their **underlying-equity OHLCV
is the on-chain fill price**. This mode emits a **PriceBar** per (symbol, bar) — the
per-asset price time-series a backtest needs (`open/high/low/close/volume/volumeUsd/ts`).
It reuses the same Alpaca `/v2/stocks/bars` leg, just at a 1-minute cadence over N days.

```bash
# 1. BACKFILL — 30 days of 1-minute bars for the Robinhood tickers, published into a
#    dataset the same MCP serves (query it by symbol; separate from the flow corpus).
quickbeam data alpaca --price-history --bar-timeframe 1Min --bar-days 30 \
  --symbols SPY,NVDA,GME,SGOV,BABA,MSTR,...   \
  --output-dir $STAGE --volume 1 --publish --namespace robinhood-prices --accumulate \
  --checkpoint-file db/alpaca_bars_day.json

# 2. EMBED + SERVE — --structured-types PriceBar makes bars INDEX+SERVE but NOT embed:
#    a 1-min bar is only ever queried by where{symbol,ts}/aggregate, so it skips the
#    embed model (the GPU bottleneck) and rides a constant placeholder vector.
quickbeam watch --source $OWNER:robinhood-prices --collection robinhood-prices \
  --cdn-dir ./cdn --cdn-domain robinhood-prices --structured-types PriceBar
```

Then from the MCP: `aggregate`/`export` with `where {symbol, ts:{gte,lte}}`.

Caveats: bars are **structured-only** (no `text`, no Asset edge — queried by their own
`symbol` field, not graph-walked). The free **IEX feed sees only IEX-routed volume**, so
`volumeUsd` understates true dollar volume (OHLC track fine); pass `--feed sip` with a
paid key for the full tape. 1-min × 30d × ~95 tickers is ~hundreds of thousands of rows —
`--bar-timeframe 5Min` or a smaller `--symbols` set trims it.

## Tests

```bash
pip install -e ".[dev]" && pytest        # pure build_graph / cursor, no network
```
