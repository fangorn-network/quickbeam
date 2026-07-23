# quickbeam-robinhood — an example pluggable Source

> Use the market-mesh MCP to pull a holistic summary of the token market. 1) Identify the top 3 best-performing and bottom 3 worst-performing tokens today. 2) Summarize current trader behavior (are they accumulation-heavy or panic-selling?). 3) Point out any clear buy or sell signals tracked in the graph, and trace the specific trigger nodes behind those signals.

A worked example of the quickbeam ingestion **harness**: a Robinhood-Chain data
source that lives in its own package and plugs into `quickbeam data …` with **no
changes to quickbeam core** (core ships zero sources on purpose).

The whole source is `quickbeam_robinhood/source.py`. It supplies only **read + shape
+ cursor**; the harness (`quickbeam.ingest.scrapers.harness`) owns everything generic
— the CLI, staged-volume emission, incremental checkpointing, the `--watch` daemon,
and `--publish` to fangorn.

| The source supplies | The harness owns |
|---|---|
| `read(cursor, args)` — live chain read (Blockscout + JSON-RPC) | argparse (shared flags), the `--watch` poll loop |
| `build_graph(records)` — pure `events → {nodes}, [edges]` | `emit_volumes` → `volume_<n>_*.json` staging |
| `next_cursor(records, prev)` — max transfer block | checkpoint file, `--accumulate` ledger merge |
| `stems` / `snapshot_stems` / `role_map` / `presentation` | `--publish` → `fangorn repo init` + `commit` + `push` |

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
  
# 3. LIVE LEDGER — daemonize: re-read every 120s, growing a superset ledger so the
#    watcher never tombstones prior flow (see --accumulate).
#    --with-holders adds the ownership shape (activeHolders/topHolderShare/seenSupplyShare).
#    It costs a bounded extra call leg per token and is what stops a raw `holders` count
#    from being read as adoption.
quickbeam data robinhood --with-transfers --with-holders --watch --poll-interval 20 \
  --output-dir $STAGE --volume 1 --publish --namespace robinhood \
  --accumulate --checkpoint-file db/robinhood_ingest_block.json --max-transfers 100

# 4. EMBED + SERVE — read the namespace back off-chain, embed, ship CDN deltas.
#    OWNER is the publisher wallet address that step 2 published under — `fangorn repo
#    init robinhood` prints it ("Owner: 0x…"), as does `fangorn status` in the repo dir.
export OWNER=0x147c24c5Ea2f1EE1ac42AD16820De23bBba45Ef6
quickbeam watch --source 0x147c24c5Ea2f1EE1ac42AD16820De23bBba45Ef6:robinhood \
  --collection robinhood \
  --cdn-dir ./cdn \
  --cdn-domain robinhood
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
  weeks. (A time-windowed read mode is the fix if that matters for your queries.)

It's computed **purely** from the events already read — no extra RPC — and is strictly
informational: it never gates ingest or moves the cursor. A source opts in by implementing
the optional `freshness_report(records, cursor)` hook (see `source.py`).

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

## What maps to the live chain

Robinhood Chain mainnet (id 4663). Live today: **Asset** snapshots (symbol, name,
price, market cap, holders, supply, address) and, with `--with-transfers`, real
**Transfer** flow (+ **Wallet** endpoints). The `CorporateAction` / `OracleUpdate` /
`LiquidityRebalance` / `NewsSentiment` branches in the shaper are scaffolding for
off-chain sibling feeds — the graph shape is ready, but nothing emits them yet.
