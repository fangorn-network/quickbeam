# 🌲 Sherwood Market Chain Topology | 2026-07-16 18:31 CDT

> **Graph State Summary:** The dominant force in this graph is **not a market force** — it is a handful of super-connector wallets recycling inventory across nearly every listed node. **97 of 98 symbols** carry a `circularityRatio` above **0.5**, and a single mint-fed wallet pair pushes **$829K** across **77 assets** into **1–2 receivers**. This corpus measures the *tokenized wrapper*, not the underlying equities: there is **no options, crypto-price, yield, or order-book data in it at all**, so the majority of the requested report structure has no data source and has not been fabricated below.

---

## ⚠️ 0. Data Availability — Read First

The requested template assumes an options/correlation/order-book feed. This corpus is Robinhood Chain (id 4663) **tokenized-stock snapshots + ERC-20 transfer flow**. Field vocabulary, verified via `describe`:

`price, holders, marketCap, totalSupply, recentVolume(Usd), netVolume(Usd), sector, signal, manipulationScore, circularityRatio, senderHHI, interArrivalCV, amountQuantization, uniqueSenders, uniqueReceivers, distinctCounterparties, fromAddr, toAddr, value, usdValue, txHash, blockNumber`

| Requested Section | Status | Reason |
| :--- | :---: | :--- |
| Options gravity wells / gamma / IV / strikes | 🔴 **NO DATA** | Zero options records. No `strike`, `expiry`, `openInterest`, `impliedVolatility`, or greeks fields exist. |
| Crypto-Equity Bridge (BTC/ETH) | 🔴 **NO DATA** | `sector="crypto"` contains only **COIN, CRCL, MSTR** — crypto-*adjacent equities*. No BTC/ETH price nodes. |
| Yield & Equity Divergence | 🔴 **NO DATA** | No bond-yield nodes. `SGOV` is present as a tokenized ETF, but carries no yield field. |
| Correlation Bridges / Beta / Decoupling | 🔴 **NO DATA** | No beta, no correlation, no returns history. Only 11 snapshots over ~2.5h — too short to compute. |
| Liquidity Vacuums / order-book depth | 🔴 **NO DATA** | No bid/ask or depth fields. Robinhood 24-Hour Market book is not in this corpus. |
| Relative Volume ("X× normal") | 🟡 **PARTIAL** | `recentVolume` is a rolling last-100-transfer window with **no historical baseline** — a ratio to "normal" is not computable. |
| Sector clusters, holders, flow, manipulation | 🟢 **SUPPORTED** | Reported below. |

> 🚨 **Anything in this file stated as a number is traceable to a query. No strikes, expiries, IV levels, or price targets are given, because inventing them for a trading decision is the one failure mode this report refuses.**

---

## 🕸️ 1. Core Market Hubs & Sector Clusters

**Corpus:** 17,507 records — 15,051 Transfers, 1,378 Wallets, 1,078 Asset snapshots = **98 unique symbols × 11 snapshots**.
Snapshot age at analysis: **18 min** (`created_at` 1784244694). Publisher `0x147c24c5Ea2f1EE1ac42AD16820De23bBba45Ef6`.

> ⚠️ **Dedupe warning:** naive `sum()` over Asset records inflates every figure ~11×. All values below are `max()` per symbol across snapshots.

Sector totals (deduped to latest snapshot per symbol):

| Sector Cluster | Symbols | Peak Holders (top node) | Network Status | Dominant Flow Character |
| :--- | :---: | :---: | :---: | :--- |
| **Mega-Cap Tech / AI** | `NVDA`, `AAPL`, `GOOGL`, `META`, `MSFT` | AAPL **10,642** | 🔴 | Wash-dominated; `senderHHI = 1.0` on AAPL/GOOGL/META — **one sender is the entire flow** |
| **Semiconductors** | `AMD`, `INTC`, `MU`, `TSM`, `AVGO`, `MRVL` | AMD **6,509** | 🔴 | AMD: 721 transfers from only **20 senders** → 137 receivers; `senderHHI = 1.0` |
| **ETF / Index** | `SPY`, `QQQ`, `SGOV`, `SLV`, `USO` | SPY **2,604** | 🔴 | SPY `circularityRatio = 0.996`, SLV **0.9999** — near-total round-tripping |
| **Retail Momentum** | `TSLA`, `PLTR`, `GME`, `COIN`, `IONQ`, `RGTI` | TSLA **7,758** | 🔴 | TSLA `circ = 0.997`, `senderHHI = 1.0`, 808 transfers / **22 senders** |
| **Crypto-Adjacent** | `COIN`, `MSTR`, `CRCL` | COIN **4,878** | 🟡 | MSTR only **23 holders** — negligible on-chain distribution |

**Highest on-chain USD flow (Transfer-side, summed `usdValue`):**

| Symbol | Transfers | Σ USD | Unique Senders | Unique Receivers | Read |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **USO** | 225 | **$1,014,875** | 20 | 18 | 🔴 `circ = 0.84` — 20 wallets generate $1M of "volume" |
| **BABA** | 100 | **$471,585** | 27 | 30 | 🔴 `circ = 0.75` |
| **QQQ** | 135 | **$308,735** | 42 | 52 | 🔴 `circ = 0.90` |
| **NVDA** | 810 | **$306,979** | 97 | 220 | 🟡 **Widest genuine counterparty spread in the corpus** |
| **SGOV** | 101 | **$297,123** | 33 | 44 | 🔴 `circ = 0.67` |
| **AMD** | 721 | **$209,810** | 20 | 137 | 🔴 20 senders / 721 transfers |
| **TSLA** | 808 | **$120,586** | 22 | 131 | 🔴 `circ = 0.997` |

---

## ⛓️ 2. The Options Chain Network (Gravity Wells)

🔴 **SECTION VOID — NO OPTIONS DATA EXISTS IN THIS CORPUS.**

A semantic search for `"options chain open interest strike expiry implied volatility gamma"` returned **zero options records**; the top hit was the **iShares 0-3 Month Treasury Bond ETF (SGOV)** at score **0.645** — the vector index reaching for the nearest unrelated text. There are no strikes, no expiries, no open interest, no IV surface, and no put/call ratio anywhere in the 17,507 records.

> 💡 **Systemic Friction Alert:** Producing a gamma-wall table here would require inventing every cell. Gamma walls, pinning levels, and IV states presented as analysis would be indistinguishable from real signal to anyone reading the Sherwood UI, and would feed directly into position sizing. **Withheld deliberately.** If options topology is required, this dataset cannot serve it — an options-chain feed must be wired into the pipeline first.

---

## 🌉 3. Correlation Bridges & Capital Flows (Edges)

The requested bridges (crypto, yield, sentiment-chain) are **not computable** — see §0. What *is* in the graph is a far more decisive edge structure:

### 🔴 The Super-Connector Wallets (the real hubs)

Grouping all 15,051 Transfers by `fromAddr` — the top senders are not traders, they are **distribution machinery touching most of the asset universe**:

| From Address | Transfers | Distinct Assets | Distinct Receivers | Σ USD |
| :--- | :---: | :---: | :---: | :---: |
| `0x0000…0000` (**mint**) | 487 | **77** | **2** | **$829,090** |
| `0xcfAEce21…A60A94` | 481 | **76** | **1** | **$829,103** |
| `0x1A18a8b9…f1FAA4E7` | 437 | **73** | 5 | $637,492 |
| `0x6d56Ab47…B87Ba158` | 893 | **70** | 223 | $207,734 |
| `0xfac1d7dC…dd8190F16` | 650 | **70** | **1** | $164,795 |
| `0x006102b1…3D7D24fa` | 536 | **77** | 71 | $142,202 |
| `0x5AaDb19C…088c5D3b` | 3,802 | 6 | 120 | **$36.40** |
| `0x2459DedB…20bDF11bf` | 2,805 | 13 | 273 | **$1.86** |

**The structural reads:**

* 🔴 **Mint → single-wallet funnel.** The zero-address mint sends **$829,090** across 77 assets into **2** receivers. `0xcfAEce21` then sends **$829,103** across 76 assets into **1** receiver. Those totals differ by **$13** — this is one inventory being passed straight through, not two independent flows.
* 🔴 **The same wallet set recurs across 60–77 *different* assets.** Per the corpus's own authenticity heuristic, this is the strongest available wash/farming flag: cross-asset wallet reuse at this breadth means the "volume" on nearly every ticker is **the same few actors**.
* 🔴 **Dust spam inflates transfer counts.** `0x5AaDb19C` sent **3,802 transfers worth $36.40 total**; `0x2459DedB` sent **2,805 transfers worth $1.86**. Together that is **44% of all transfers in the corpus** and **essentially zero economic value**. Any per-asset `recentTransfers` count is contaminated by this.
* 🔴 **Quantized/dust values repeat mechanically.** Value `0` appears **215×** across 12 symbols; value `1.1e-7` appears **39×** across 7 symbols from **2** senders; `7e-8` **39×**; `2.6e-7` **39×**. Identical micro-amounts repeating across unrelated tickers is scripted emission, not trading.

---

## 🚨 4. Network Anomalies & Structural Findings

### ⚡ Corpus-Wide Circularity (the headline anomaly)

`circularityRatio` across all 98 symbols, at peak snapshot:

| Band | Symbols |
| :--- | :--- |
| 🔴 **> 0.99** (near-total round-trip) | `SLV` 0.9999, `META` 0.998, `TSLA` 0.9968, `SPY` 0.9957, `USAR` 0.9956, `AVGO` 0.9925 |
| 🔴 **0.85 – 0.99** | `QQQ` 0.897, `MSTR` 0.887, `RDW` 0.883, `AMAT` 0.883, `GOOGL` 0.871, `AAPL` 0.980, `NBIS` 0.868, `XNDU` 0.935, `LITE` 0.935 |
| 🟡 **0.5 – 0.85** | `USO` 0.841, `TSM` 0.818, `BABA` 0.752, `SGOV` 0.675, `AMD` 0.667, `NVDA` 0.733 |

**Only `NVDA` shows a counterparty spread (97 senders → 220 receivers) inconsistent with a small closed loop.** It is the single node in this graph whose flow is plausibly multi-party — and even it carries `circ = 0.73`.

### 🔄 The `signal` Field Is Unstable — Do Not Trust It

`signal` distribution: **active-mixed 1,007** / **active-organic 54** / **quiet 17**.

The `active-organic` label is **not a stable property of an asset** — it flips between snapshots of the *same symbol* within one hour, because every derived metric is computed over a **rolling last-100-transfer window** (`sampleSize: 100`):

* **AAPL** — `circularityRatio` reads **0.0** in one snapshot and **0.98** in another; `recentVolumeUsd` **$782.95** vs **$16,077**.
* **INTC** — `circ` **0.0** (organic) vs **0.676** (mixed).
* Symbols labeled organic do so on **near-zero-volume windows**: `MSFT` $5.63, `ORCL` $0.31, `CRWV` $0.13, `COIN` $0.11, `USAR` $0.02.

> 🟡 **`active-organic` here mostly means "the window happened to be empty," not "this asset trades cleanly."** Screening on `signal` will systematically select the *least* active windows.

### 🟡 Price Field Integrity

`price` mirrors the real equity price via Blockscout `exchange_rate` and is the corpus's most trustworthy field — but two values do not survive a sanity check and should be treated as suspect before use:

* **`SNDK` = $6,982.05** — implausible on its face.
* **`MU` = $966.40**, **`AMD` = $532.77**, **`NFLX` = $71.00** — flagged for verification against a real quote source. (NFLX is plausible post-split; the others are not verifiable from this corpus.)

### 🔴 Wrapper ≠ Company

`marketCap`, `holders`, `totalSupply`, `recentVolume` describe **only the tokenized wrapper**. `SGOV` shows `marketCap $808,044` on `totalSupply 8,028` tokens. **`MSTR` has 23 on-chain holders.** None of these are equity fundamentals and must never be read as such.

---

## 🩺 5. Pipeline Health — Action Required

Two of the four required processes are degraded. This materially affected the analysis:

| Process | Status | Impact |
| :--- | :---: | :--- |
| Ingest daemon (pid 8389) | 🟢 Running | Snapshot 18 min old — fresh |
| CDN serve (pid 12440) | 🟢 Running | — |
| MCP server (pid 12572) | 🟢 Running | `aggregate` / `get` work correctly |
| **`watch_robinhood.sh`** | 🔴 **NOT RUNNING** | **Embeddings are not being refreshed into Qdrant** |

**🔴 Semantic search is effectively broken right now, and this is downstream of the dead watcher:**

* `search("NVIDIA AI chip maker", entity_type="Asset")` → **zero results**, despite NVDA carrying 810 transfers and 11 snapshots.
* `search("bitcoin ethereum crypto treasury proxy")` → returned **SGOV** ×6 at score 0.70.
* `search("options chain … gamma")` → returned **SGOV** at 0.645.

Every semantic query collapses onto SGOV or returns nothing — the vector index is stale/partial because nothing has re-embedded. **All findings in this report were derived via `aggregate`/`describe`, which read the in-memory record set and are unaffected.**

Also note: the skill's documented node id form **`rh:asset:NVDA` does not resolve** — `neighbors()` on it returns `[]`. Record ids in this corpus are CIDs (e.g. `bafyreid6nsjmov…`). The relational axis could not be walked by symbol.

**Fix, in order:**
1. Restart `./watch_robinhood.sh` (chain tip → embed → Qdrant → CDN delta, 60s).
2. If the `robinhood` Qdrant collection was wiped, **also delete `db/robinhood_checkpoint.json`** or nothing re-embeds.
3. Re-run any semantic screening only after the index rebuilds.

---

## 🏹 6. What This Graph Actually Supports

Not trading triggers — the data cannot carry them. What it supports:

* 🟦 **Treat every `recentVolume` figure in this corpus as unverified until the circularity is netted out.** At `circ > 0.99` (SPY, TSLA, META, SLV), the reported volume is approximately **one actor moving inventory in a loop**. The tradeable read is that on-chain volume here is **not** demand.
* 🟦 **`NVDA` is the only node worth further flow analysis** — 97 senders / 220 receivers is the corpus's one genuine distribution. Start there if the question is "does any tokenized equity have real participation?"
* 🟦 **Adoption, not flow, is the honest metric.** `holders` is the least gameable field present: AAPL 10,642 · SPCX 10,569 · GOOGL 9,301 · TSLA 7,758 · PLTR 6,875 · AMD 6,509. Rank distribution on this, not on volume.
* 🟦 **Discard the two dust senders before any transfer-count analysis.** `0x5AaDb19C` and `0x2459DedB` contribute **6,607 transfers (44% of the corpus) worth $38.26 combined**.

---

### 🔗 Provenance

All records published by **`0x147c24c5Ea2f1EE1ac42AD16820De23bBba45Ef6`** on Robinhood Chain (4663). Snapshot `created_at` **1784244694** (2026-07-16 18:31:34 CDT), analyzed 18 min later. Representative verifiable commit CIDs:

* `bafyreidcxk7jjvbddnkmobqmhn5oxt7w3fcmqj7mzz5xmeqcfxk56m5lie` — SGOV @ $100.65, observed 1784218658
* `bafyreid6nsjmovgtms464jnmzkqfmdvuc33aeksjj44k6y5bf25x5oe67y` — SGOV, observed 1784216656
* `bafyreif5afzp4qh6jo6zhbajrdaypxe5gxlcz6ly6oln2zawvqkbes2xc4` — SGOV, observed 1784214238

> Aggregate figures (§1, §3, §4) are reductions over the full in-memory record set at snapshot 1784244694 and are reproducible via the `aggregate` calls named inline. **Because the watcher is down, re-running semantic `search` will not reproduce §5's failures once the index rebuilds — that section is a point-in-time observation.**
