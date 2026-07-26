# quickbeam-robinhood — an example pluggable Source

> Use the market-mesh MCP to pull a holistic summary of the token market. 1) Identify the top 3 best-performing and bottom 3 worst-performing tokens today. 2) Summarize current trader behavior (are they accumulation-heavy or panic-selling?). 3) Point out any clear buy or sell signals tracked in the graph, and trace the specific trigger nodes behind those signals.

A worked example of the quickbeam ingestion **harness**: a Robinhood-Chain data
source that lives in its own package and plugs into `quickbeam data …` with **no
changes to quickbeam core** (core ships zero sources on purpose).

It shapes up to four entity types in **one crawl**: **Asset** (live token snapshots),
**Transfer** + **Wallet** (real on-chain flow, `--with-transfers`), and **PriceBar** (a
bounded per-symbol price history, `--with-prices`). The chain has no price *history*, so
one Alpaca fetch does double duty: it emits the PriceBar series (regular time grid, real
30-day coverage on every asset regardless of transfer volume) **and** fills each Transfer's
event-timed `price` — same crawl, one command, no second pipeline.

The whole source is `quickbeam_robinhood/source.py`. It supplies only **read + shape
+ cursor**; the harness (`quickbeam.ingest.scrapers.harness`) owns everything generic
— the CLI, staged-volume emission, incremental checkpointing, the `--watch` daemon,
and `--publish` to fangorn.

| The source supplies | The harness owns |
|---|---|
| `read(cursor, args)` — live chain read (Blockscout + JSON-RPC), + Alpaca close per transfer (`--with-prices`) | argparse (shared flags), the `--watch` poll loop |
| `build_graph(records)` — pure `events → {nodes}, [edges]` | `emit_volumes` → `volume_<n>_*.json` staging |
| `next_cursor(records, prev)` — max transfer block | checkpoint file, `--accumulate` ledger merge |
| `stems` / `snapshot_stems` / `role_map` / `presentation` | `--publish` → `fangorn repo init` + `commit` + `push` |

## Quick start

> **The one flag everyone forgets: `--with-prices`.** It *is* the price pipeline — it fills
> `Transfer.price` and emits `PriceBar`. Without it the crawl is Blockscout-only, so
> `Transfer.price` is null and there are no PriceBars (flat `usdValue = value × current`).

```bash
# 0. Install this source AND the alpaca bars reader (imported lazily by --with-prices).
cd example-robinhood-source
pip install -e . && pip install -e ../example-alpaca-source

# 1. Alpaca keys — REQUIRED by --with-prices. Put them in example-robinhood-source/.env
#    (auto-loaded from cwd or beside the package; the crawl FAILS LOUDLY if they're missing):
#      APCA_API_KEY_ID=...
#      APCA_API_SECRET_KEY=...          # free IEX keys: https://alpaca.markets

# 2. CRAWL + PUBLISH — note the --with-prices. Prints the publisher wallet ("Owner: 0x…").
quickbeam data robinhood --with-transfers --with-holders --with-prices \
  --max-transfers 500 \
  --output-dir ./stage_volumes --volume 1 --publish --namespace robinhood \
  --accumulate --checkpoint-file db/robinhood_ingest_block.json

# 3. VERIFY the price leg actually ran (before embedding). Both must be true:
ls stage_volumes/volume_1_pricebars.json          # PriceBar rows were staged
grep -o '"price":[0-9.]*' stage_volumes/volume_1_transfers.json | head   # transfers carry price
#   Zero PriceBars / no "price" here  ⇒  --with-prices didn't run (missing flag or creds).

# 4. EMBED + SERVE — --structured-types PriceBar keeps bars indexed but NOT embedded.
export OWNER=<the Owner: 0x… printed in step 2>
quickbeam watch --source $OWNER:robinhood --collection robinhood \
  --cdn-dir ./cdn --cdn-domain robinhood --structured-types PriceBar
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
quickbeam mcp --cdn-url http://localhost:8090 --transport http --port 8765
```

Then from the MCP: `aggregate`/`export` over `PriceBar` with `where {symbol, ts:[t0,t1]}`
for the 30-day price series, and `Transfer.price` moves at each transfer's block time.

## Install

```bash
# into the same environment as quickbeam
cd example-robinhood-source
pip install -e .          # or: pip install -e ".[dev]" to run the tests
```

`discover_sources()` reads the `quickbeam.sources` entry-point group, so once this is
installed the verb appears automatically:

```bash
quickbeam data robinhood --help
```

## Use

```bash
export STAGE=./stage_volumes

# 1. INGEST — one live read → staged node/edge volumes (this is all `data` does).
quickbeam data robinhood --with-transfers --output-dir $STAGE --volume 1
#   --with-transfers adds real on-chain Transfer flow (a 2nd entity type + edges);
#   --max-assets N caps the universe; --dry-run previews the embed text, writes nothing.
#   --max-transfers N keeps the newest N transfers per token (live tail); for the full
#   time-ranged history a backtest needs, use --transfer-since-days (see Deep transfer
#   history below).

# 2. PUBLISH — assemble the volumes into one {vertices,edges} batch and settle it into a
#    namespace, following fangorn's git-native model:
#      fangorn repo init <ns>     (idempotent — tracks the namespace, allocates if new)
#      fangorn commit <batch> -m  (snapshots the batch into a local commit)
#      fangorn push               (settles that commit as the on-chain state root)
#    Needs the `fangorn` CLI on PATH (harness default --fangorn-bin is "fangorn") and a
#    configured wallet (`fangorn init`, or ETH_PRIVATE_KEY/PINATA_* env). Pass
#    --fangorn-bin "<full command>" only if you run fangorn via a wrapper (dotenvx/node).
quickbeam data robinhood --with-transfers --output-dir $STAGE --volume 1 \
  --publish --namespace robinhood


  # --with-prices is what fills Transfer.price + emits PriceBar (needs Alpaca keys; see Quick start).
  quickbeam data robinhood --with-transfers --with-holders --with-prices \
  --output-dir ./stage_volumes --volume 1 --publish --namespace robinhood \
  --accumulate --checkpoint-file db/robinhood_ingest_block.json --max-transfers 500
  
# 3. LIVE LEDGER — daemonize: re-read every 120s, growing a superset ledger so the
#    watcher never tombstones prior flow (see --accumulate).
#    --with-holders adds the ownership shape (activeHolders/topHolderShare/seenSupplyShare).
#    It costs a bounded extra call leg per token and is what stops a raw `holders` count
#    from being read as adoption.
quickbeam data robinhood --with-transfers --with-holders --with-prices --watch --poll-interval 120 \
  --output-dir ./stage_volumes --volume 1 --publish --namespace robinhood \
  --accumulate --checkpoint-file db/robinhood_ingest_block.json --max-transfers 1000

# note: if the commit step fails, run the following
cd /home/driemworks/fangorn/embeddings/example-robinhood-source
../venv/bin/python -c "import quickbeam as qb; from quickbeam_robinhood.source import RobinhoodSource; qb.Publisher(RobinhoodSource(), namespace='robinhood', output_dir='./stage_volumes', volume=1).publish()"


# 4. EMBED + SERVE — read the namespace back off-chain, embed, ship CDN deltas.
#    OWNER is the publisher wallet address that step 2 published under — `fangorn repo
#    init robinhood` prints it ("Owner: 0x…"), as does `fangorn status` in the repo dir.
export OWNER=0x147c24c5Ea2f1EE1ac42AD16820De23bBba45Ef6
quickbeam watch --source 0x9c3Feac9eaD11D89E9Ed1f00ceE4B85ACc00E7d2:robinhood \
  --collection robinhood \
  --cdn-dir ./cdn \
  --cdn-domain robinhood \
  --structured-types PriceBar          # index PriceBars but don't embed them

quickbeam cdn bake --collection robinhood --cdn-dir ./cdn --domain robinhood --config domains.json

quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
# start the mcp 
quickbeam mcp --cdn-url http://localhost:8090 --transport http --port 8765
```

## Freshness — where the live tail sits in time

Every ingest cycle prints a **freshness readout** (and, with `--checkpoint-file`, persists
it there under `<name>Freshness`) so you can see how current the tail is without
reverse-engineering it from the staged JSON:

```
[freshness] where the live tail sits in time
  head block 9,790,534  ·  at head (newest tracked event is current)
  newest tracked event  blk 9,790,534  (15s ago)
  new this cycle: 52 transfer(s) over blk 9,789,701→9,790,534 (span 84s)
  asset flow age  <1h:96  1-24h:2  1-7d:0  >7d:0  none:0   (98 assets)
  ~0.100 s/block (calibrated from this read)
```

It answers the two DISTINCT staleness questions separately:

- **Lag (am I current?)** — `head − newest tracked event`, in blocks and estimated
  wall-time (seconds/block is self-calibrated from the block↔timestamp pairs in the read,
  no hard-coded constant). Because the source reads **newest-first**, lag is bounded by
  `--poll-interval`, not a growing backlog. NB: we track only ~98 tokens, so raw head-lag
  counts every *other* chain block too and **overstates** staleness — the honest liveness
  number is *newest tracked event … N ago*.
- **Coverage (how much did this cycle pull, and how evenly?)** — the block/time span of the
  transfers emitted this cycle, plus a per-asset last-activity age histogram. This is where
  the **count-window's uneven temporal reach** shows up: `--max-transfers N` grabs the
  *newest N per token*, so a hot token shows only its last hour while a quiet one spans
  weeks. The fix when you need real depth is the **deep backfill** below.

It's computed **purely** from the events already read — no extra RPC — and is strictly
informational: it never gates ingest or moves the cursor. A source opts in by implementing
the optional `freshness_report(records, cursor)` hook (see `source.py`).

## Deep transfer history (`--transfer-since-days` / `--transfer-since-block`)

`--max-transfers N` caps at **ingestion**: it pulls the newest N *real* transfers per token
and emits only the largest — liquid names (SPY, GME, SGOV) reach back only hours, and the
deeper history is never pulled. For a backtest you need every transfer in a time window,
uncapped. Backfill mode pages Blockscout backward (newest-first, cursor-paginated — there's
no server-side block filter) until it crosses the floor, and emits **every in-window
transfer**, not just the largest:

```bash
# One-shot: 30 days of ALL transfers per token. Use a FRESH/absent --checkpoint-file — this
# is a historical backfill, not the live tail. Heavy: liquid tokens are tens of thousands
# of rows, and transfers embed by default.
quickbeam data robinhood --with-transfers --transfer-since-days 30 \
  --output-dir $STAGE --volume 1 --publish --namespace robinhood
#   --transfer-since-block N  = a hard block floor instead (wins over --transfer-since-days).
```

Because backfilled transfers can be tens of thousands per token and Transfer nodes embed by
default, mark them structured-only on the watcher if you only `where{symbol,ts}` over them:
`quickbeam watch … --structured-types Transfer` (skips the embed model, keeps them
filterable — the same lever the price leg uses). The flow metrics on each Asset
(`recentVolume`, `circularityRatio`, `manipulationScore`, …) are then computed over the
**whole window** rather than the newest hours.

## Price history (`--with-prices`)

The chain gives a live price *scalar* but no *history*, so `usdValue = value × one current
price` is **dead-flat across every time bin** — there's nothing to trade against. Since the
tokens mirror real equities 1:1, one Alpaca fetch fixes it two ways, both from the same
bars:

1. **PriceBar** — a bounded per-symbol OHLCV series on a **regular time grid** (default
   5-minute), the real 30-day history a backtest needs. Coverage is complete on *every*
   asset, wash-traded or not, because it's sampled on time, not on transfer activity.
2. **Transfer.price** — each Transfer's `price`/`usdValue` filled with the close **at its
   on-chain timestamp**, so recent flow is priced at the moment it happened (not a flat
   current scalar). Irregular, but honest; bin/forward-fill on your side.

Why two layers: transfers are the *wrong* carrier for history because their density tracks
trading, not time — a wash-traded token (NVDA does ~600k transfers/**day**) gives only
*minutes* of span before the row count explodes, while a dormant token spans weeks. PriceBar
sidesteps that entirely; Transfer.price stays for pricing the flow you actually capture.

**Credentials (the #1 gotcha).** Needs free Alpaca keys, and the quickbeam CLI does **not**
auto-load a shell env — so keys you forgot to `export` silently produced flat prices. This
source now **auto-loads a `./.env`** (from cwd or beside the package) and **fails loudly** if
creds are still missing. Put them in `example-robinhood-source/.env`:

```
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...        # free IEX keys: alpaca.markets
```

```bash
pip install -e ../example-alpaca-source                # the bars reader (imported lazily)

quickbeam data robinhood --with-transfers --with-prices \
  --output-dir $STAGE --volume 1 --publish --namespace robinhood
#   --bar-timeframe 5Min (default) ≈160k bars for 99 tickers × 30d; 1Hour ≈16k (coarser),
#   1Min ≈800k (finest). Prices the SAME tickers this crawl discovered.
```

Query PriceBars and priced transfers alike with `where {symbol, ts:[t0,t1]}` / `aggregate`.
PriceBars are **structured-only** (no `text`, no edge) — mark them on the watcher so they
index but don't embed:

```bash
quickbeam watch --source $OWNER:robinhood --collection robinhood \
  --cdn-dir ./cdn --cdn-domain robinhood --structured-types PriceBar
```

Symbols with no equity (the `ROBIN` native token, an unlisted squat) get no bars and their
transfers stay **unpriced** — honestly absent, not back-fabricated.

**Deeper raw transfers** (optional, and *not* needed for price history): `--transfer-since-days
N` pages transfers back N days, stopping at the `--max-transfers` ceiling **or** the time
floor, whichever first (set `--max-transfers`, e.g. 2000; `0` = uncapped, careful). Blockscout
has no block filter, so it's slow on liquid tokens (logs progress so it's not mistaken for a
hang) — but on a wash token even this only buys minutes of span, which is exactly why price
history lives in PriceBar, not here.

## From Python (the SDK path — no entry point needed)

A `Source` is also usable directly via `quickbeam.Publisher`:

```python
import quickbeam as qb
from quickbeam_robinhood import RobinhoodSource

pub = qb.Publisher(RobinhoodSource(), namespace="robinhood")   # fangorn_bin defaults to "fangorn"
pub.run(with_transfers=True, max_transfers=500)   # ingest → publish (repo init + commit + push)
```

## Test

```bash
pip install -e ".[dev]"
pytest tests/
```

`build_graph` is pure, so the tests hand-build events and assert on the shaped graph —
no network. See `tests/test_robinhood.py`.

## What the embeddings carry (and why)

The whole point of this source is a corpus of vectors you can reason over, so the
shaping is deliberate about **what belongs in the embedded text vs. in structured
fields**, and about **time**:

- **Semantic content → the embedded `text` blurb.** For an Asset that's the curated
  business profile (what the company *does* — this is what makes "AI chip makers" or
  "quantum computing" retrieve the right names) plus a real activity line ("Actively
  traded: N recent on-chain transfers moving ~$X"). For a Transfer it's the USD
  notional and the **real block time** ("…~$494.60… on 2026-07-12 13:42 UTC"), so
  "large recent flow" retrieves whales, not dust.
- **Time is honest, never wall-clock-as-event-time.** An Asset is a *live quote* with
  no event time of its own, so it is **not** stamped with a read-time `blockTimestamp`
  (that made every quote read as "happened now"). Instead it carries `observedAt` (when
  we read it — indexed as staleness metadata, kept out of the blurb) and, with flow,
  `lastActivityAt`/`lastActivityBlock` (the real chain time of its latest transfer — a
  true freshness anchor). Only discrete Transfer events carry an event `timestamp`/
  `blockNumber`, so only they are sequenced.
- **Magnitude is legible at every scale.** These are 18-decimal fractional-share
  tokens, so real flow is often sub-dollar; USD and token amounts use adaptive
  precision ("~$0.01", "0.000014 AMD") so a genuine small transfer isn't crushed to
  "$0"/"0.00" and read as a null event.
- **Prices are event-timed, and history is its own layer.** With `--with-prices` a
  Transfer's `price`/`usdValue` is the Alpaca close AT its on-chain timestamp (so `usdValue`
  moves across time instead of `value × one current price`), and the same fetch emits
  **PriceBar** rows — a bounded per-symbol series on a regular time grid. Both are
  structured-only (no `text`, filtered by `where{symbol,ts}`, never embedded): mark
  `PriceBar` (and deep `Transfer` backfills) on the watcher's `--structured-types` so they
  index but skip the embed model.

## What maps to the live chain

Robinhood Chain mainnet (id 4663). Live today: **Asset** snapshots (symbol, name,
price, market cap, holders, supply, address) and, with `--with-transfers`, real
**Transfer** flow (+ **Wallet** endpoints). With `--with-prices`, each transfer is priced at
its block time and a **PriceBar** history series is emitted (see above). The
`CorporateAction` / `OracleUpdate` / `LiquidityRebalance` / `NewsSentiment` branches in the
shaper are scaffolding for off-chain sibling feeds — the graph shape is ready, but nothing
emits them yet.

Note the Asset `price` is a single **live scalar** (Blockscout `exchange_rate`), not a
series — there's no price *history* on-chain. Because these tokens mirror real equities
1:1, the underlying-equity bars **are** the price, which is what `--with-prices` sources
from Alpaca (into PriceBar rows, and onto each Transfer).
