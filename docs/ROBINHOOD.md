# Robinhood-Chain Ingestion for Fangorn / quickbeam

> **Owner + namespace model.** Fangorn no longer has a schema registry, bundles, or
> linksets — publishing writes tagged vertices/edges into a namespace under the
> publisher's own on-chain root, and reading goes through `fangorn read`/`head`
> (shelled out to by quickbeam). The Quickstart below is current; deeper sections
> further down that still say `fangorn commit --bundle`/`push`, `--repo`, or `schemagen`
> describe the pre-rewrite model and are pending a fuller pass — the command shapes
> in the Quickstart and in the main [README](../README.md) are the source of truth.

## Quickstart — grow a live transfer ledger

Bring the four processes up **in order**. The critical flag is **`--accumulate`** on
the ingest daemon: without it, every commit is a full-replacement "newest-250"
snapshot, and the watcher's delete-propagation garbage-collects the old flow — pinning
the index at ~300 points no matter how long you run. `--accumulate` merges new
transfers into the staged files so each commit is a **superset**, so the watcher drops
nothing and the index grows. (Asset price quotes always replace wholesale — latest
wins, stable ids upsert.)

**Depth lever — `--max-transfers`:** transfer reads are paginated, so this is how
much real flow you capture per token per cycle. The default (5) emits only the 5
largest transfers per token; raise it (e.g. `500`) to walk Blockscout's transfer
pages and pull genuine on-chain volume. This, times `--accumulate`, is what turns a
few-hundred-point sampler into a deep ledger.

```bash
# setup env vars
export STAGE=~/fangorn/embeddings/stage_volumes

# 1. Ingest daemon — chain → fangorn repo init/upload, as a GROWING LEDGER.
#    --checkpoint-file makes each cycle read only new flow instead of re-scanning.
#    Look for "mode=ledger (accumulate)" and per-cycle "Transfer : N (+k new)".
quickbeam data robinhood --with-transfers --watch --poll-interval 120 \
  --output-dir $STAGE --volume 1 --publish --namespace robinhood \
  --accumulate --checkpoint-file db/robinhood_ingest_block.json \
  --max-transfers 500

# 2. Watcher — fangorn head/read → embed → Qdrant → CDN delta.
#    export ROBINHOOD_OWNER=0x... (the publisher wallet) first — see watch_robinhood.sh.
./watch_robinhood.sh

# 3. CDN — serve the baked domain + delta shards.
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors

# 4. MCP — the tool surface agents query.
quickbeam mcp --cdn-url http://localhost:8090 --transport http --port 8765
```

**Switching an existing (snapshot-mode) deployment to ledger mode:** the index was
built from replace-snapshots, so reset it once so the watcher rebuilds from the
growing superset tip (wipe the collection **and** its checkpoint together, or nothing
re-embeds):

```bash
curl -s -X DELETE http://localhost:6333/collections/robinhood
rm -f db/robinhood_checkpoint.json          # then restart ./watch_robinhood.sh
```

The ledger grows **forward** from launch — it starts near the current newest-250 and
climbs as new transfers land; it does not backfill history the snapshots already
dropped. To seed deeper history, run a one-shot `--start-block <N>` before enabling
the daemon.

---

  Running processes (background tasks of this session — if you restart the machine, bring them
  back in this order):
  1. quickbeam data robinhood --with-transfers --watch --publish --namespace robinhood --poll 30
  chain → fangorn
  1. ./watch_robinhood.sh
  namespace → embeddings → CDN
  1. quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
  2. quickbeam mcp --cdn-url http://localhost:8090 --transport http --port 8765

**Status:** Reads the **live** Robinhood Chain (mainnet, id 4663) end-to-end.
- **Owner+namespace pipeline (the only path):** `data robinhood [--watch --publish]` → `fangorn repo init` + `upload` → `watch --source`. The ingest daemon keeps reading the RPC and publishing fresh snapshots to the wallet's `robinhood` namespace, `watch` embeds new/changed vertices → Qdrant → CDN delta. Every read is against the live chain — there is no fixture / mock mode. ✅

**Pitch shift:** from a *local business directory tool* (`sond3r`) to a
**High-Performance, Low-Latency Financial Knowledge CDN** for on-chain AI trading
agents on Robinhood Chain — the "Financial Knowledge Mesh for Agentic Finance."

---

## 1. The problem this solves

If thousands of autonomous trading agents keep their own knowledge graphs / vector
DBs fresh, they hit an architectural wall:

- **The RPC nightmare** — every agent polling the sequencer / indexing nodes to
  re-verify state means RPC bottlenecks, rate limits, and latency. In trading, a
  30-second lag is financial ruin.
- **The centralized API trap** — a central gateway streaming context over
  websockets means egress costs that destroy the agent network's margins.

**Fangorn decouples heavy data crunching from the trading execution loop.** The data
flow — note the subgraph is *downstream of us*, populated when we publish via fangorn,
not an upstream Robinhood feed we read:

```
Robinhood Chain mainnet (id 4663)          off-chain feeds
  · token universe + live price/mcap/         · corporate actions
    holders/supply via Blockscout API         · news sentiment
  · block height via JSON-RPC               (Robinhood API / news wires)
        │  read                                     │
        └──────────────────┬─────────────────────── ┘
                           ▼
quickbeam data robinhood        shape → staged node/edge volumes
        │
        ▼
fangorn commit --bundle / push  "ingest via fangorn": WRITES on-chain, EMITS
        │                        DataSource-registry events
        ▼
Subgraph indexer                indexes Fangorn's events  ◄── WE populate this
        │
        ▼
quickbeam watch --bundle        reads the (Fangorn) subgraph → embed → Qdrant
        │
        ▼
Static Edge CDN ── Delta Shard (.ndjson.gz) ──► Trading Agents
                   (~KB manifest sync; pull only the delta into local Qdrant)
```

**Connecting to Robinhood Chain** (mainnet, live): chain id **4663**, RPC
`https://rpc.mainnet.chain.robinhood.com`, explorer
`https://robinhoodchain.blockscout.com` (Blockscout). Tokenized stocks are ERC-20s
named `"<Company> • Robinhood Token"` (AAPL, NVDA, TSLA, MSFT, META, GOOGL, SPY,
QQQ, …). The reader pulls the universe + live `exchange_rate` / market cap / holders
/ supply from the Blockscout API and block height from RPC — these defaults are
built in (`ROBINHOOD_RPC_URL` / `ROBINHOOD_BLOCKSCOUT` in `pipelines/robinhood.py`).

Three value props:

1. **Zero-egress financial delta streaming** — when a stock token has a corporate
   action, oracle shift, or liquidity rebalance, Fangorn writes a tiny compressed
   delta shard (`.ndjson.gz`) to a static CDN edge. The agent polls a small
   `manifest.json`, downloads just the delta, and appends it to its local execution
   graph. No re-downloading full market snapshots.
2. **Live vector edge-sync for market sentiment** — the embedding pipeline runs
   server-side (`watch`). Fangorn watches on/off-chain events, auto-embeds the
   sentiment, and streams raw embedding deltas into the agent's edge-side vector DB
   over cheap HTTP. Continuous vectorized market awareness with no local embedding.
3. **Cryptographically verifiable alpha** — the pipeline registers each update as an
   on-chain **commit** with a Merkle root, so agents can verify the cryptographic
   origin of the knowledge they consume, defeating data-poisoning attacks.

---

## 2. Key architectural finding

**Most of the pitch already existed in this repo.** The three pillars map onto
machinery built for the local-business pipeline:

| Pitch claim | Existing component | How Robinhood uses it |
|---|---|---|
| ~KB manifest sync + delta shards (`.ndjson.gz`) | `cdn.append_domain` / `write_delta_shard` — content-addressed immutable shards + a tiny mutable `manifest.json` served `no-cache` | Unchanged; source-agnostic |
| Live vector edge-sync (server-side embed → stream deltas) | `watcher` → `_embed_and_upload` → `append_domain` | `watch --bundle` follows the on-chain tip |
| Cryptographically verifiable alpha (on-chain manifest provenance) | `objects.py` commit/tree/blob Merkle-DAG + `resolve_tip_commit` + delete-propagation, driven by `fangorn commit`/`push` | publishes a versioned bundle commit |

The only thing coupled to "local business directory" was the **ingestion adapter**.
So "ingest Robinhood data" = **write one new `data` pipeline that shapes events into
the same staged node/edge volumes** every other source produces — nothing downstream
changes.

### Separation of concerns (the important correction)

quickbeam keeps ingest / publish / embed / serve as distinct stages, and this
integration respects that boundary:

| Stage | Command | Responsibility |
|---|---|---|
| **ingest** | `quickbeam data robinhood` | events → staged `volume_<n>_*.json` node/edge files. **Nothing else.** |
| **publish** | `fangorn commit --bundle` + `fangorn push` | volumes → on-chain commit (IPFS blobs + tip move) |
| **embed + ship** | `quickbeam watch --bundle …` | tip → embeddings → Qdrant → CDN delta shard |
| **serve** | `quickbeam cdn serve` | static shard delivery to edge agents |

> An earlier draft wrongly made `data robinhood` do ingest **and** embed **and**
> CDN. That collapsed three stages into the `data` command. It's fixed: `data
> robinhood` now only shapes + stages, exactly like `data placespg` / `data
> eventspg`; embedding + delivery is `watch`'s job.

---

## 3. The graph model

`build_graph` turns a batch of events into a small typed graph: **one `Asset` node
per symbol** (latest snapshot wins; a symbol seen only via an event gets a minimal
`{symbol, name, sector}` Asset synthesized), and **each discrete event as its own node
linked from its Asset by a typed edge**.

### What you can actually ingest today (both from the live chain)

| `type` | Node | Edge | Real source |
|---|---|---|---|
| `asset` | `Asset` | — (it *is* the Asset) | on-chain token metadata + Blockscout price / mcap / holders / supply |
| `transfer` | `Transfer` | `hasTransfer` | on-chain ERC-20 Transfer flow (`--with-transfers`) |

That is the whole live graph right now: a plain run gives you **Assets**; adding
`--with-transfers` adds the **Transfer** flow (+ `recentVolume`/`recentTransfers`
onto each Asset). `Transfer` is deliberately kept **out** of the `asset` root
profile's fold (whale-move text is embedding noise) — it embeds as its own record and
links by an edge.

### Scaffolded but NOT wired to any source yet

The shaper *also* understands four more event types, but **nothing produces them yet**
— there is no reader that emits them. They're scaffolding that shows how an **off-chain
feed would hang off the same graph** — each needs a real integration that doesn't exist
yet, and some may not map to anything on Robinhood Chain at all:

| `type` | Node | Edge | Would require |
|---|---|---|---|
| `corporate_action` | `CorporateAction` | `hasAction` | a corporate-actions data source (splits/dividends) |
| `news_sentiment` | `NewsSentiment` | `hasNews` | a news / sentiment API |
| `oracle_update` | `OracleUpdate` | `hasOracleUpdate` | a price-oracle feed (price here is Blockscout-derived, not an on-chain event) |
| `liquidity_rebalance` | `LiquidityRebalance` | `hasLiquidity` | a DEX/AMM pool (not confirmed to exist on this chain) |

Until a reader emits these `type`s, **a live run contains only Asset (+ Transfer)
nodes**. The `verbalize` / `shape_event` branches for these four exist (and are
unit-tested with hand-built events) so the graph shape, schemas, and embeddings are
ready the moment a real feed is added. Treat these four as a design placeholder, not a
capability.

### Embedding

The **`asset` root profile** (in `embeddings.ROOT_PROFILES`) is configured to fold
the off-chain event nodes into the Asset document *when present* — so on today's live
data (Asset + Transfer, Transfer excluded) it folds nothing and the Asset embeds from
its own blurb. Each event node also embeds as its own record, so a query can hit it
directly. Every node carries a `text` blurb (embedded) plus facets (`symbol`, `name`,
`sector`, `signal`) and structured measures (`price`, `marketCap`, `holders`,
`recentVolume`, …) indexed for hybrid filtering, **not** folded into the prose.

**The Asset blurb leads with a business description.** Blockscout gives price / mcap /
holders but no description of what a company *does*, so a bare stat-line ("NVDA is a
tokenized semiconductors stock trading at $194.57…") embeds nearly identically across
all 50 tickers — the vectors collapse and semantic search returns the same names for
every query. `verbalize` therefore prepends a curated one-sentence profile per ticker
(`_PROFILES` in `pipelines/robinhood.py`, e.g. *"NVIDIA designs the GPUs and AI
accelerators that power data-center machine learning…"*). This is what lets queries
like *"AI chip makers"*, *"quantum computing"*, *"bitcoin treasury"* or *"space &
satellites"* actually retrieve the right stocks. New listings fall through to the
stat-line until a profile is added.

---

## 4. The pipeline — git-native provenance

The pure shaper (`shape_event` / `verbalize`, dependency-free, unit-tested) feeds the
single, canonical delivery path. Git-native fangorn ships it: `commit --bundle`/`push`
version typed graphs on-chain, and `watch` inherits the commit's embed contract. Every
update is a parented commit with a Merkle root → verifiable alpha (pitch #3). Reuses
`schemagen` + `watch` + `cdn` **unchanged**.

> **The subgraph is ours, downstream.** We don't read a "Robinhood subgraph" — there
> isn't one. We READ Robinhood Chain data (JSON-RPC `eth_getLogs` over the
> tokenized-stock / oracle contracts) + off-chain feeds, and INGEST via fangorn.
> `fangorn commit`/`push` writes on-chain, which emits the DataSource-registry events
> a subgraph indexer picks up — so *we populate the (Fangorn) subgraph by publishing*,
> and `watch --bundle` reads it.
>
> **What's on-chain vs off-chain.** The reader hits the real Robinhood Chain (id 4663).
> On-chain today: **Asset snapshots** (symbol, name, price, market cap, holders,
> supply, token address) and — with `--with-transfers` — real **ERC-20 Transfer
> flow** (the token contracts emit `Transfer` / `TransferWithScaledUI`; e.g. TSLA has
> ~1,800 transfers). There is **no on-chain oracle-price or discrete corporate-action
> event** — price is Blockscout-computed and corporate actions are baked into the
> tokens' balance-scaling. So `OracleUpdate` / `CorporateAction` / `LiquidityRebalance`
> / `NewsSentiment` are **off-chain** feeds (Robinhood API / news wires) still to wire.
> Without `--with-transfers`, a live run is Asset-only (one entity type, no edges) —
> which is correct, just thin.

---

## 5. Usage

```bash
cd ~/fangorn/embeddings && source venv/bin/activate
export STAGE=~/fangorn/embeddings/stage_volumes
```

### Ingest → publish → embed → serve (verifiable)

A one-time **bootstrap** (dump → schemas → repo → first commit), then a **live ingest
daemon** that keeps reading the RPC and pushing fresh snapshots to Fangorn.

**Bootstrap (once):**

```bash
# 1. INITIAL DUMP — one live read → staged node/edge volumes (data only shapes+stages)
quickbeam data robinhood --with-transfers --output-dir $STAGE --volume 1
#    --with-transfers adds real on-chain Transfer flow (2nd entity type + edges);
#    omit it for Asset snapshots only. --max-assets N caps; --rpc-url/--blockscout-url override.

# 2. SCHEMAGEN — infer one node schema per entity type + the bundle shape, written to
#    $STAGE/schemas/ (default out-dir). E.g. robinhood.chain.asset.v1.json,
#    robinhood.chain.transfer.v1.json, … + the bundle robinhood.chain.market.v1.json.
quickbeam data schemagen --input-dir $STAGE --volume 1 \
  --prefix tony.robinhood.chain --bundle-name market --version v1

# 3. INIT REPO → COMMIT (which registers) → PUSH. You do NOT register schemas one by
#    one: `commit --bundle` reads $STAGE/schemas/fangorn_schemas.json and registers
#    every node schema + the bundle idempotently, then commits the whole graph in one
#    go. `repo init -s <bundle>` works before on-chain registration because a bundle
#    schema id is DETERMINISTIC (computed from the local definition schemagen wrote).
#    You're typing the repo against the BUNDLE — which spans all node types + edges —
#    not one node schema.
fangorn repo init rh-market -s tony.robinhood.chain.market.v1
fangorn commit --bundle $STAGE --volume 1 -m "robinhood market test" \
  --embed-model nomic-ai/nomic-embed-text-v1.5 --embed-dim 256
fangorn push
```

**Live ingest daemon (long-running):** reads the RPC every `--poll-interval` seconds
and re-`commit`+`push`es the current snapshot. Prices/holders move each cycle, and
fangorn's structural sharing re-uploads only the tokens that changed.

```bash
cd ~/fangorn/embeddings
quickbeam data robinhood --watch --poll-interval 120 \
  --output-dir $STAGE --volume 1 \
  --publish --repo ~/fangorn/embeddings/
#   (drop --publish to only re-write volumes and let an external cron do commit/push;
#    or run this one-shot from cron every N min instead of --watch)
```

**Embed + serve (separately, long-running):** builds embeddings off the on-chain tip
each poll and ships CDN deltas.

```bash
# EMBED + SHIP — fold transfer flow into each Asset doc AND embed each Transfer as
# its own record; follow the tip. (Or just run ./watch_robinhood.sh, which is this.)
quickbeam watch --bundle "tony.robinhood.chain.market.v1=0xe97604fb475049b60de4209534fe3aa0d5109f642bad203bf8f79ab0e8bdc7f8" \
  --root-profile asset --root-profile transfer --collection robinhood \
  --checkpoint-file ./db/robinhood_checkpoint.json \
  --role-map-file ./db/robinhood_role_map.json \
  --cdn-dir ./cdn --cdn-domain robinhood --poll-interval 30 $BUILD_AUTH
# NOTE: the checkpoint file tracks what's already embedded. If you wipe the
# `robinhood` collection (or want a full rebuild), delete the checkpoint too —
# otherwise every manifest is considered done and nothing re-embeds.

# SERVE — static delta CDN for edge trading agents
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
```

So two daemons run side by side: `data robinhood --watch --publish` (chain → Fangorn)
and `watch --bundle` (Fangorn tip → embeddings → CDN). Each snapshot is a parented
commit, so `fangorn log` shows the full price history and `watch` tombstones anything
dropped between versions.

### Ingest-command flags (`data robinhood`)

Every read is against the **live chain** — the defaults (`ROBINHOOD_RPC_URL` /
`ROBINHOOD_BLOCKSCOUT`) point at Robinhood Chain mainnet; there is no fixture mode.

| Flag | Default | Purpose |
|---|---|---|
| `--rpc-url`, `--blockscout-url` | built-in | override the live chain endpoints |
| `--max-assets` | 0 | cap tokenized stocks read (0 = all) |`
| `--with-transfers` | off | also read on-chain Transfer flow (Transfer nodes + edges + Asset volume) |
| `--max-transfers` | 5 | largest recent transfers to emit per token |
| `--block-gt` | 0 | only ingest events with `blockNumber >` this |
| `--output-dir`, `--volume` | `./stage_volumes`, 1 | where to write the volume files |
| `--dry-run` | off | print shaped nodes + embed text; write nothing |
| `--watch` | off | run as a daemon: re-read + re-emit every `--poll-interval`s |
| `--poll-interval` | 120 | seconds between reads in `--watch` (block time ~101s) |
| `--publish` | off | after writing volumes, `fangorn commit --bundle` + `push` |
| `--repo` | `.` | fangorn repo dir to run commit/push in |
| `--fangorn-bin`, `--commit-message` | `fangorn`, auto | publish CLI path / message |

### Interface — the `examples/` app against the robinhood domain

The web UI is the Vite app in `examples/`. Select the shard data source with
`VITE_DATA_SOURCE=shards` (the default is `mock`!) and point it at the served
`robinhood` CDN domain via `VITE_CDN_URL` (where `cdn serve` is listening) and
`VITE_DOMAIN` (see `examples/src/lib/config.ts` — note: `VITE_CDN_DOMAIN` is the
*playground* app's variable, not this one's):

```bash
cd ~/fangorn/embeddings/examples
npm install                              # first time only

# DEV — live-reload UI against the running `cdn serve` (port 8090 above):
VITE_DATA_SOURCE=shards VITE_CDN_URL=http://localhost:8090 VITE_DOMAIN=robinhood npm run dev
#   → open the printed http://localhost:5173

# PRODUCTION build:
npm run stage:cdn -- --domain robinhood  # bake the domain's shards into public/
npm run build                            # static bundle → examples/dist/
npm run preview                          # serve the built bundle locally to check it
```

The UI reads the same static manifest + delta shards a trading agent pulls, so it is a
faithful view of what's on the CDN — no separate backend.

---

## 6. Verification

**Live chain read** (`data robinhood`) against Robinhood Chain mainnet:

```
[robinhood] read 50 tokenized stock(s) from Robinhood Chain (head block 1448563)
   50 stocks: AAPL, AMAT, AMD, AMZN, APLD, ASML, ASTS, BABA, BE, COIN, COST, DDOG,
              GOOGL, META, MSFT, NFLX, NVDA, ORCL, PLTR, QCOM, QQQ, SOFI, SPY, TSLA, …
   · rh:asset:NVDA  →  NVIDIA (NVDA) is a tokenized semiconductors stock trading at
                       $194.42. Market cap $800,717. 97 on-chain holders.
   · rh:asset:TSLA  →  Tesla (TSLA) … trading at $394.70 …
```

Discovery uses Blockscout's token **name search** (`?q=Robinhood Token`) so tokens with
a null market cap (NFLX, COST, SOFI, …) are included. (The plain market-cap-sorted list
drops those — its cursor stalls at the null-mcap tail, which is the bug that used to cap
this at 32.) Blockscout's cursor stalls after one 50-item page, so if the catalog ever
exceeds 50 we'd need to enumerate from a registry/factory contract instead.

Real fields per Asset node: `price` (live `exchange_rate`), `marketCap`, `holders`,
`totalSupply`, `address`, `sector`. (Live yields Asset snapshots, plus the Transfer
flow with `--with-transfers`; the four off-chain event types await their feeds.)

**Ingest → schemagen** (live chain, with the Transfer flow for a 2nd entity + edges):

```
$ quickbeam data robinhood --with-transfers --output-dir stage_rh --volume 1
   ✅ Asset    : 50 → volume_1_assets.json
   ✅ Transfer :  9 → volume_1_transfers.json
   ✅ edges    :  9 → volume_1_edges.json

$ quickbeam data schemagen --input-dir stage_rh --volume 1 \
    --prefix test.robinhood.chain --bundle-name market --version v2
   ✅ Asset    → test.robinhood.chain.asset.v2     (13 fields)
   ✅ Transfer → test.robinhood.chain.transfer.v2  (…)
   ✅ hasTransfer  Asset → Transfer
   📦 bundle 'test.robinhood.chain.market.v2' (1 edge shape)
```

The bundle is ready for `fangorn commit --bundle`.

**Semantic search** (after `watch --bundle` embeds the tip and `cdn serve` is up) — the
curated business profiles make the corpus discriminate by investment thesis:

```
$ curl 'localhost:8080/search?q=AI+chip+and+semiconductor+makers&n_results=5'
   NVDA 0.756 · INTC 0.741 · MU 0.733 · AMD 0.731 · TSM 0.720
$ curl 'localhost:8080/search?q=quantum+computing'
   RGTI 0.689 · IONQ 0.676 · …
$ curl 'localhost:8080/search?q=space+rockets+and+satellites'
   ASTS 0.744 · SPCX 0.721 · RKLB 0.685 · RDW 0.638
```

`append_domain` ships **only new points** as content-addressed delta shards, leaving
prior shards immutable (hard HTTP cache hits) — the pitch's #1/#2.

**Unit tests:** `quickbeam/pipelines/test_robinhood.py` — **9 passing** (record shape
for all 5 types, asset-id idempotency vs. event uniqueness, oracle deviation/signal,
sentiment tone, asset blurb leads with the business profile, role-map text composition,
`build_graph` asset-dedup + edge linking, `emit_volumes` file output).

---

## 7. Files

| File | Change |
|---|---|
| `quickbeam/pipelines/robinhood.py` | **new** — pure shaper (`verbalize` + `_PROFILES`), live `_read_robinhood_chain` reader, `build_graph`/`emit_volumes` (ingest), publish leg |
| `quickbeam/pipelines/test_robinhood.py` | **new** — 9 unit tests |
| `quickbeam/cli.py` | **edit** — registered `quickbeam data robinhood` (ingest only) |
| `quickbeam/ingest/graph/projection.py` | **edit** — added the `asset` root profile to `ROOT_PROFILES` |

---

## 8. Open items / next steps

1. **Off-chain event feeds.** The live reader (`_read_robinhood_chain`) covers the
   on-chain layer: the tokenized-stock universe + prices (Asset snapshots) and, with
   `--with-transfers`, ERC-20 Transfer flow. Corporate actions, oracle updates,
   liquidity and news sentiment are **off-chain** — add sibling readers (Robinhood API
   / news wires) that emit the same raw-event dicts so `build_graph` links them to
   their Asset. Until then, live runs produce Asset (+ Transfer) nodes; the four
   off-chain `verbalize`/`shape_event` branches stay unit-tested and ready. (This
   reader is the *upstream Robinhood source* — distinct from the Fangorn subgraph
   `watch --bundle` consumes, which we populate by publishing via `fangorn commit`.)
2. **Holder / concentration signal.** `/api/v2/tokens/{addr}/holders` exposes the
   ownership distribution — a top-holder-share / concentration measure on each Asset
   (or a Holder node graph) is a natural next on-chain enrichment.
3. **End-to-end on a live commit.** The `fangorn commit --bundle` → `push` →
   `watch --bundle` legs were verified structurally (schemagen output is valid) but
   not against a real on-chain tip; run once with chain creds + Pinata to confirm the
   `asset` root profile folds context as intended.
4. **Finance-specific payload indexes.** `ensure_indexes` indexes business/event
   fields. Add float/keyword indexes for `price`, `deviationPct`, `sentiment`,
   `sector` so agents can do hybrid queries like "bearish news on energy tokens with
   a >5% oracle move."
5. **Client pull loop.** Confirm `pull.py` incrementally pulls a served `robinhood`
   domain into a local Qdrant for the full round-trip agent demo.
