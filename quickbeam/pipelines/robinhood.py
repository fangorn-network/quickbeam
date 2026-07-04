"""
robinhood.py — Robinhood-Chain financial events → a Fangorn graph.

WHERE THIS SITS IN THE PIPELINE
-------------------------------
quickbeam has a clean separation of concerns and this module respects it:

  ingest        `quickbeam data robinhood`   events → staged node/edge volumes
  publish       `fangorn commit --bundle`     volumes → on-chain commit (IPFS + tip)
  embed + ship  `quickbeam watch --bundle`    tip → embeddings → Qdrant → CDN delta
  serve         `quickbeam cdn serve`         static shard delivery to edge agents

So `data robinhood` ONLY shapes + stages data — exactly like `data placespg` /
`data eventspg`. It never embeds and never touches the CDN; that is `watch`'s job.

ONE PATH — GIT-NATIVE PROVENANCE
--------------------------------
The pure shaper (`shape_event` / `verbalize`, dependency-free, unit-tested) feeds
the single, canonical delivery path:

  Emit volume files → `schemagen` → `fangorn commit --bundle` → `push` → `watch
  --bundle robinhood=0x<id> --root-profile asset`. Publishing via fangorn is what
  WRITES this data on-chain; that emits the DataSource-registry events a subgraph
  indexer picks up, and `watch` reads that subgraph. Every update is a parented
  commit with a Merkle root, so a trading agent can cryptographically verify the
  origin of the knowledge it consumes (defeats data-poisoning). Reuses `watch` /
  `cdn` unchanged.

READING ROBINHOOD DATA (the upstream source)
--------------------------------------------
We READ Robinhood Chain data and INGEST it via fangorn — we do NOT read a "Robinhood
subgraph" (there isn't one). The subgraph in the pipeline is *Fangorn's own*: it
indexes the DataSource-registry events emitted when `fangorn commit --bundle`/`push`
publishes this data on-chain. So WE POPULATE that subgraph by publishing, and `watch
--bundle` reads it — the subgraph is downstream of us, not an upstream Robinhood feed.

The upstream Robinhood source is Robinhood Chain mainnet (id 4663): the
tokenized-stock universe + live prices come from its Blockscout explorer API (the
chain's own indexer), and block height from JSON-RPC. Off-chain feeds (corporate
actions, news sentiment) plug in as sibling readers. `read_robinhood_events` always
reads the real chain via `_read_robinhood_chain` (`--rpc-url` / `--blockscout-url`
override the mainnet defaults).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# ROLE MAP + PRESENTATION — the canonical field roles for a Robinhood record,
# used by `compose_searchable_text` and the unit tests. (In the live pipeline the
# server infers roles from the committed bundle, so `watch` doesn't read these.)
#
# The high-value semantic signal is the verbalized `text` blurb; price/deviation
# are structured measures for hybrid filtering, not embedded prose.
# ---------------------------------------------------------------------------
ROBINHOOD_ROLE_MAP: dict = {
    "title":    "symbol",
    "subtitle": "name",
    "tags":     ["sector", "actionType", "signal"],
    "text":     ["text"],
}

ROBINHOOD_PRESENTATION: dict = {
    "accent": "#00c805",  # Robinhood green
    "icons": {
        "Asset":              "trending_up",
        "CorporateAction":    "account_balance",
        "OracleUpdate":       "bolt",
        "LiquidityRebalance": "water_drop",
        "NewsSentiment":      "newspaper",
    },
}

ENTITY_TYPES = ("Asset", "CorporateAction", "OracleUpdate",
                "LiquidityRebalance", "NewsSentiment")

# Event type → (entityType, edge relation from its Asset). `transfer` is real
# on-chain flow (ERC-20 Transfer events): the `asset` root profile folds each
# transfer's verbalized blurb into its Asset doc (see ROOT_PROFILES in
# embeddings.py — `content_fields: ["text"]`), and each transfer ALSO embeds as
# its own record via the `transfer` profile, linked by a hasTransfer edge. The
# other event types are scaffolding for off-chain sibling feeds (corporate
# actions, news sentiment) — none of them exist on-chain today.
_EVENT_SPEC = {
    "corporate_action":    ("CorporateAction",    "hasAction"),
    "oracle_update":       ("OracleUpdate",       "hasOracleUpdate"),
    "liquidity_rebalance": ("LiquidityRebalance", "hasLiquidity"),
    "news_sentiment":      ("NewsSentiment",      "hasNews"),
    "transfer":            ("Transfer",           "hasTransfer"),
}
_TYPE_TO_ENTITY = {"asset": "Asset", **{k: v[0] for k, v in _EVENT_SPEC.items()}}


# ---------------------------------------------------------------------------
# PURE SHAPER — one raw event dict → one node's fields. No I/O.
# ---------------------------------------------------------------------------
def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pct(a, b):
    """Signed percent change b→a, or None if b is falsy."""
    a, b = _num(a), _num(b)
    if not b:
        return None
    return round((a - b) / b * 100.0, 3)


def _iso_to_epoch(s):
    """Blockscout ISO-8601 timestamp (e.g. "2024-01-15T12:34:56.000000Z") → epoch
    seconds, or None if absent/unparseable. Blockscout stamps each transfer with the
    real on-chain block time — this is what lets downstream sequence flow (holding
    periods, before/after splits) instead of guessing from read order."""
    if not s:
        return None
    try:
        import datetime as _dt
        return int(_dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# BUSINESS PROFILES — one factual sentence per ticker describing WHAT the company
# does. Blockscout gives us price/holders/market-cap but no description, so every
# Asset would otherwise embed the same "<name> is a tokenized <sector> stock"
# boilerplate — collapsing all 50 vectors into one cluster (identical scores,
# undiscriminating search). Folding this line into the embedded text is what lets
# "AI chip makers", "quantum computing", "bitcoin treasury", "space & satellites"
# etc. actually retrieve the right names. Curated & deterministic; keyed by symbol.
# New listings simply fall through to the stat-line until a profile is added.
# ---------------------------------------------------------------------------
_PROFILES: dict[str, str] = {
    "AAPL": "Apple designs consumer electronics — the iPhone, Mac and Apple Watch — plus a large software and services business.",
    "AMAT": "Applied Materials makes the wafer-fabrication equipment and materials-engineering tools that chip foundries use to manufacture semiconductors.",
    "AMD":  "AMD designs CPUs, GPUs and data-center accelerators for AI, gaming and servers, competing with Intel and NVIDIA.",
    "AMZN": "Amazon runs the largest e-commerce marketplace and AWS, the leading cloud-computing platform.",
    "APLD": "Applied Digital builds and operates AI data centers and high-performance GPU compute hosting for machine-learning workloads.",
    "ASML": "ASML is the sole maker of EUV lithography machines — the essential equipment for fabricating advanced semiconductors.",
    "ASTS": "AST SpaceMobile is building a satellite constellation that beams broadband directly to ordinary cell phones from space.",
    "BABA": "Alibaba is China's largest e-commerce and cloud-computing group.",
    "BE":   "Bloom Energy makes solid-oxide fuel cells for clean, on-site electricity and hydrogen power.",
    "CLSK": "CleanSpark is a bitcoin mining company running large-scale, energy-efficient crypto mining data centers.",
    "COIN": "Coinbase operates the largest US cryptocurrency exchange and custody platform for bitcoin and digital assets.",
    "COST": "Costco is a warehouse-club retailer selling groceries and bulk consumer staples on a membership model.",
    "CRCL": "Circle issues USDC, the leading regulated dollar stablecoin, and provides crypto payments infrastructure.",
    "CRWV": "CoreWeave is a specialized GPU cloud provider renting NVIDIA accelerators for AI training and inference.",
    "DDOG": "Datadog provides cloud observability and monitoring software for infrastructure, applications and security.",
    "EWY":  "The iShares MSCI South Korea ETF tracks a basket of large South Korean equities.",
    "FLNC": "Fluence Energy builds grid-scale battery energy-storage systems and software for the electric grid.",
    "GLW":  "Corning makes specialty glass and ceramics — optical fiber, display glass and Gorilla Glass for phones.",
    "GME":  "GameStop is a video-game and consumer-electronics retailer, and a well-known meme stock.",
    "GOOGL":"Alphabet owns Google — the dominant search engine and digital-advertising business — plus Google Cloud and AI research.",
    "INTC": "Intel designs and manufactures CPUs and is building a contract chip-foundry business.",
    "IONQ": "IonQ builds trapped-ion quantum computers and sells quantum computing over the cloud.",
    "IREN": "IREN runs renewable-powered data centers for bitcoin mining and AI cloud compute.",
    "LITE": "Lumentum makes lasers, optical components and photonics for telecom, datacom and 3D sensing.",
    "META": "Meta operates Facebook, Instagram and WhatsApp, earns from advertising, and invests heavily in AI and VR/AR.",
    "MSFT": "Microsoft makes Windows, Office and the Azure cloud platform, and is a lead investor in OpenAI.",
    "MSTR": "Strategy (formerly MicroStrategy) is a business-intelligence software firm that holds bitcoin as its primary treasury reserve.",
    "MU":   "Micron makes memory and storage chips — DRAM, NAND and high-bandwidth memory (HBM) for AI accelerators.",
    "NBIS": "Nebius Group provides AI-focused cloud infrastructure and GPU compute, spun out of Yandex.",
    "NFLX": "Netflix is the leading subscription streaming service for films and television.",
    "NNE":  "Nano Nuclear Energy is developing small modular and microreactor nuclear power technology.",
    "NVDA": "NVIDIA designs the GPUs and AI accelerators that power data-center machine learning, gaming and autonomous systems — the bellwether of the AI compute cycle.",
    "ORCL": "Oracle sells enterprise databases and is expanding aggressively into cloud infrastructure for AI.",
    "PLTR": "Palantir builds big-data analytics and AI software for government, defense and enterprise customers.",
    "QCOM": "Qualcomm designs mobile chipsets and 5G modems that power most smartphones.",
    "QQQ":  "Invesco QQQ is an index ETF tracking the Nasdaq-100, heavily weighted toward large technology stocks.",
    "RDW":  "Redwire makes space infrastructure — satellite components, solar arrays and in-space manufacturing.",
    "RGTI": "Rigetti Computing builds superconducting quantum computers and quantum cloud services.",
    "RKLB": "Rocket Lab launches small satellites to orbit and builds spacecraft and space systems.",
    "SGOV": "The iShares 0-3 Month Treasury Bond ETF holds ultra-short US T-bills as a cash-equivalent, low-risk instrument.",
    "SLV":  "The iShares Silver Trust is a commodity ETF backed by physical silver bullion.",
    "SNDK": "Sandisk makes NAND flash memory, SSDs and storage products.",
    "SOFI": "SoFi is a digital bank and fintech offering lending, investing and banking through an app.",
    "SPCX": "SpaceX designs and launches reusable rockets and operates Starlink satellite internet.",
    "SPY":  "The SPDR S&P 500 ETF tracks the S&P 500 — broad exposure to US large-cap stocks.",
    "TSLA": "Tesla makes electric vehicles, batteries and solar energy products and develops autonomous-driving technology.",
    "TSM":  "Taiwan Semiconductor (TSMC) is the world's largest contract chip foundry, manufacturing the most advanced semiconductors.",
    "TTWO": "Take-Two Interactive is a video-game publisher behind Grand Theft Auto and NBA 2K via its Rockstar and 2K studios.",
    "USAR": "USA Rare Earth develops domestic rare-earth mining and magnet manufacturing for critical-minerals supply.",
    "USO":  "The United States Oil Fund is a commodity ETF that tracks the price of crude oil.",
}


def verbalize(ev: dict) -> str:
    """Human-readable blurb for an event — this is what gets embedded. Deterministic
    (no wall-clock in the text) so a re-shaped event embeds identically."""
    t = ev.get("type")
    sym = ev.get("symbol", "?")
    name = ev.get("name") or sym
    sector = ev.get("sector") or "equity"
    if t == "asset":
        parts = [f"{name} ({sym}) is a tokenized {sector} stock"]
        if ev.get("price") is not None:
            parts.append(f" trading at ${_num(ev.get('price'), 0):.2f}")
        if ev.get("dayChangePct") is not None:
            parts.append(f", day change {_num(ev.get('dayChangePct'), 0):+.2f}%")
        parts.append(".")
        if ev.get("marketCap") is not None:
            parts.append(f" Market cap ${_num(ev.get('marketCap'), 0):,.0f}.")
        if ev.get("holders") is not None:
            parts.append(f" {int(_num(ev.get('holders'), 0))} on-chain holders.")
        # Lead with the business description (if we have one) so the embedding is
        # dominated by WHAT the company does, not the shared stat-line boilerplate.
        profile = _PROFILES.get(sym)
        return (profile + " " + "".join(parts)) if profile else "".join(parts)
    if t == "corporate_action":
        detail = ev.get("detail") or ""
        return (f"Corporate action on {name} ({sym}): {ev.get('actionType', 'action')}. "
                f"{detail} Ex-date {ev.get('exDate', 'TBD')}.").strip()
    if t == "oracle_update":
        dev = _pct(ev.get("newPrice"), ev.get("oldPrice"))
        if dev is not None:
            return (f"Oracle price update for {name} ({sym}): "
                    f"${_num(ev.get('oldPrice'), 0):.2f} → ${_num(ev.get('newPrice'), 0):.2f} "
                    f"({dev:+.2f}% move) via {ev.get('oracle', 'oracle')}.")
        return (f"Oracle price update for {name} ({sym}) to "
                f"${_num(ev.get('newPrice'), 0):.2f}.")
    if t == "liquidity_rebalance":
        return (f"Liquidity rebalance for {name} ({sym}) in pool "
                f"{ev.get('pool', '?')}: depth "
                f"${_num(ev.get('oldDepth'), 0):,.0f} → ${_num(ev.get('newDepth'), 0):,.0f}.")
    if t == "news_sentiment":
        score = _num(ev.get("sentiment"), 0.0)
        tone = "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral"
        return (f"{tone.capitalize()} news on {name} ({sym}) "
                f"[{ev.get('source', 'wire')}]: {ev.get('headline', '')}. "
                f"{ev.get('summary', '')}").strip()
    if t == "transfer":
        frm = (ev.get("fromAddr") or "?")[:10]
        to = (ev.get("toAddr") or "?")[:10]
        return (f"Notable on-chain transfer of {_num(ev.get('value'), 0):,.2f} {sym} "
                f"({name}) from {frm}… to {to}….")
    return f"{name} ({sym}) event."


def _signal(ev: dict) -> str:
    """A coarse, filterable taste facet."""
    t = ev.get("type")
    if t == "oracle_update":
        dev = _pct(ev.get("newPrice"), ev.get("oldPrice")) or 0.0
        return "oracle-spike" if abs(dev) >= 5 else "oracle-drift"
    if t == "news_sentiment":
        s = _num(ev.get("sentiment"), 0.0)
        return "bullish" if s > 0.15 else "bearish" if s < -0.15 else "neutral"
    if t == "asset":
        dc = ev.get("dayChangePct")
        if dc is None:
            return "listed"           # live snapshot carries no intraday delta
        return "gainer" if _num(dc, 0) >= 0 else "loser"
    if t == "liquidity_rebalance":
        return "liq-inflow" if _num(ev.get("newDepth"), 0) >= _num(ev.get("oldDepth"), 0) \
            else "liq-outflow"
    if t == "corporate_action":
        return ev.get("actionType") or "action"
    if t == "transfer":
        return "notable-transfer"    # we only emit the largest recent transfers
    return t or "event"


def node_id(ev: dict) -> str:
    """Stable node id. Asset snapshots collapse to one id per symbol (a live quote
    that OVERWRITES); discrete events get a unique id so each is its own record."""
    t, sym = ev.get("type"), ev.get("symbol", "?")
    if t == "asset":
        return f"rh:asset:{sym}"
    if t == "corporate_action":
        return f"rh:ca:{sym}:{ev.get('exDate', '?')}:{ev.get('actionType', '?')}"
    if t == "oracle_update":
        return f"rh:oracle:{sym}:{ev.get('blockNumber', '?')}"
    if t == "liquidity_rebalance":
        return f"rh:liq:{sym}:{ev.get('blockNumber', '?')}"
    if t == "news_sentiment":
        h = hashlib.sha256((ev.get("headline", "") + sym).encode()).hexdigest()[:16]
        return f"rh:news:{sym}:{h}"
    if t == "transfer":
        return f"rh:xfer:{ev.get('txHash', '?')}:{ev.get('logIndex', '?')}"
    return f"rh:{t}:{hashlib.sha256(json.dumps(ev, sort_keys=True).encode()).hexdigest()[:16]}"


def shape_fields(ev: dict) -> dict:
    """Raw event → node fields (the `fields` block of a bundle node). Carries the
    verbalized `text` blurb (embedded), the ticker/sector/signal facets, and the
    type-specific structured measures (indexed for hybrid filtering)."""
    entity = _TYPE_TO_ENTITY.get(ev.get("type"), "Asset")
    fields: dict = {
        "symbol":     ev.get("symbol"),
        "name":       ev.get("name") or ev.get("symbol"),
        "sector":     ev.get("sector") or "equity",
        "actionType": ev.get("actionType") or ev.get("type"),
        "signal":     _signal(ev),
        "text":       verbalize(ev),
        "entityType": entity,
    }
    for k in ("price", "dayChangePct", "marketCap", "oldPrice", "newPrice",
              "oldDepth", "newDepth", "sentiment", "holders", "totalSupply",
              "value", "usdValue", "recentVolume", "recentTransfers"):
        if ev.get(k) is not None:
            fields[k] = _num(ev[k])
    if ev.get("address"):
        fields["address"] = ev["address"]         # on-chain token contract
    for k in ("fromAddr", "toAddr", "txHash"):     # transfer provenance
        if ev.get(k):
            fields[k] = ev[k]
    # TIME-ORDERING — stamp discrete on-chain events (transfers, actions, oracle
    # moves) with their real block time + height so the index can SEQUENCE flow:
    # holding periods, turnover, before/after splits. Asset snapshots are LIVE quotes
    # stamped at chain head — their block/ts are read-time, not an event time — so we
    # deliberately don't index them (they'd read as "everything happened now").
    if ev.get("type") != "asset":
        blk = _num(ev.get("blockNumber"))
        ts = _num(ev.get("blockTimestamp"))
        if blk is not None:
            fields["blockNumber"] = int(blk)
        if ts is not None:
            fields["timestamp"] = int(ts)
    if ev.get("type") == "oracle_update":
        dev = _pct(ev.get("newPrice"), ev.get("oldPrice"))
        if dev is not None:
            fields["deviationPct"] = dev
    return {k: v for k, v in fields.items() if v is not None}


def shape_event(ev: dict, *, owner: str = "robinhood-chain") -> dict:
    """Raw event → the universal record shape consumed by the Path A embed loop
    (`embeddings._embed_and_upload`) and `cdn`. Pure; no I/O."""
    fields = shape_fields(ev)
    blk = int(ev.get("blockNumber", 0) or 0)
    return {
        "track_id":    node_id(ev),
        "entity_type": fields["entityType"],
        "fields":      fields,
        "meta": {
            "owner":          ev.get("owner", owner),
            # No IPFS manifest in the direct path — synthesize a logical feed id.
            # The delta CDN keys shards on track_id, not this.
            "manifestCid":    f"robinhood-chain@{blk}",
            "blockNumber":    blk,
            "blockTimestamp": int(ev.get("blockTimestamp", time.time())),
        },
    }


# ---------------------------------------------------------------------------
# GRAPH — a batch of events → typed nodes + edges (the Path B bundle shape).
#
# One Asset node per symbol (latest snapshot wins; a bare {symbol,name,sector}
# node is synthesized for symbols seen only through events). Each discrete event
# is its own node, linked from its Asset by a typed edge — so the `asset` root
# profile folds an equity's actions / oracle moves / news into one document.
# ---------------------------------------------------------------------------
def build_graph(events: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Return ({entityType: [{"name", "fields"}]}, [edge...]) in the exact shape
    the `schemagen` → `fangorn commit --bundle` path consumes (mirrors places_pg)."""
    assets: dict[str, dict] = {}          # symbol -> Asset node (latest snapshot)
    asset_block: dict[str, int] = {}      # symbol -> block of the kept snapshot
    others: dict[str, list[dict]] = {}    # entityType -> node list
    wallets: dict[str, dict] = {}         # id -> Wallet node (deduped by address)
    edges: list[dict] = []
    seen_nodes: set[str] = set()

    def _ensure_asset(ev: dict) -> None:
        sym = ev.get("symbol")
        if sym and sym not in assets:
            assets[sym] = {"name": f"rh:asset:{sym}", "fields": {
                "symbol": sym, "name": ev.get("name") or sym,
                "sector": ev.get("sector") or "equity", "entityType": "Asset",
                "text": f"{ev.get('name') or sym} ({sym}) — tokenized "
                        f"{ev.get('sector') or 'equity'} stock.",
            }}

    def _ensure_wallet(addr: str | None) -> str | None:
        """Promote a bare from/to address string into a first-class Wallet node.
        The id is LOWERCASED (Blockscout returns checksummed, mixed-case hashes — a
        raw-case id would fragment one wallet into several). Deduped by that id, so
        every wallet is a single node no matter how many transfers touch it. Once a
        Transfer links to its wallets, `Wallet → Transfer → Asset` is walkable via the
        existing hasTransfer edge — no separate wallet→asset edge needed."""
        if not addr:
            return None
        wid = f"rh:wallet:{addr.lower()}"
        if wid not in wallets:
            try:
                is_mint = int(addr, 16) == 0        # 0x000…0 = ERC-20 mint/burn sentinel
            except ValueError:
                is_mint = False
            wallets[wid] = {"name": wid, "fields": {
                "entityType": "Wallet",
                "address":    addr,                 # checksummed, for display
                "signal":     "mint" if is_mint else "wallet",
                "text":       (f"Mint/burn address {addr} (ERC-20 zero address)."
                               if is_mint else f"On-chain wallet {addr}."),
            }}
        return wid

    for ev in events:
        sym = ev.get("symbol")
        blk = int(ev.get("blockNumber", 0) or 0)
        if ev.get("type") == "asset":
            # Latest snapshot per symbol wins (a live quote overwrites).
            if sym not in assets or blk >= asset_block.get(sym, -1):
                assets[sym] = {"name": node_id(ev), "fields": shape_fields(ev)}
                asset_block[sym] = blk
            continue

        spec = _EVENT_SPEC.get(ev.get("type"))
        if not spec:
            continue
        entity, rel = spec
        _ensure_asset(ev)
        nid = node_id(ev)
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            others.setdefault(entity, []).append({"name": nid, "fields": shape_fields(ev)})
        if sym:
            edges.append({"rel": rel, "from": f"rh:asset:{sym}", "to": nid,
                          "fromType": "Asset", "toType": entity})
        # Wallet endpoints: a Transfer points at its sender/receiver, making each
        # wallet a traversable node. Only transfers carry from/to addresses.
        frm, to = _ensure_wallet(ev.get("fromAddr")), _ensure_wallet(ev.get("toAddr"))
        if frm:
            edges.append({"rel": "sentBy", "from": nid, "to": frm,
                          "fromType": entity, "toType": "Wallet"})
        if to:
            edges.append({"rel": "receivedBy", "from": nid, "to": to,
                          "fromType": entity, "toType": "Wallet"})

    nodes = {"Asset": list(assets.values())}
    nodes.update(others)
    if wallets:
        nodes["Wallet"] = list(wallets.values())
    return nodes, edges


# stem name (the volume_N_<stem>.json suffix) per entity type.
_STEM = {"Asset": "assets", "CorporateAction": "corporateactions",
         "OracleUpdate": "oracleupdates", "LiquidityRebalance": "liquidity",
         "NewsSentiment": "news", "Transfer": "transfers", "Wallet": "wallets"}

# Stems rewritten wholesale every cycle vs. accumulated into a growing ledger.
# Assets are LIVE snapshots keyed on a stable id (rh:asset:SYM) — the latest quote
# wins, so replacing the file each cycle is correct (the index upserts by id). Every
# other stem is a stream of DISCRETE on-chain events (transfers, actions): with
# --accumulate we MERGE new rows into the staged file (dedup by node name, existing
# rows kept in place) so each fangorn commit is a SUPERSET of the last. That
# superset property is what stops the watcher's delete-propagation from garbage-
# collecting flow that scrolled out of Blockscout's newest-N window — see watcher's
# tombstone_commit_delta: a replace-snapshot makes it delete everything the new
# commit dropped, pinning the index at ~300; a superset commit drops nothing.
_SNAPSHOT_STEMS = {"assets"}


def _load_node_list(path: str) -> list[dict]:
    """Existing staged nodes/edges (empty list if absent/unreadable). A corrupt
    staged file must not stall ingest — we start that stem fresh instead."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception as e:  # noqa: BLE001
        print(f"[robinhood] staged file {path!r} unreadable ({e}); starting stem fresh",
              file=sys.stderr)
        return []


def _merge_keep_order(existing: list[dict], new: list[dict], key) -> tuple[list[dict], int]:
    """Append rows from `new` whose key isn't already present, preserving the
    existing order (so fangorn's content-addressed chunking reuses unchanged
    chunks — old rows stay byte-identical). Returns (merged, added_count)."""
    seen = {key(r) for r in existing}
    merged = list(existing)
    added = 0
    for r in new:
        k = key(r)
        if k not in seen:
            seen.add(k)
            merged.append(r)
            added += 1
    return merged, added


def emit_volumes(events: list[dict], output_dir: str, volume: int = 1,
                 accumulate: bool = False) -> dict:
    """INGEST OUTPUT — write the staged node/edge volume files `schemagen` reads.
    This is all `quickbeam data robinhood` does. Returns a per-type count summary.

    accumulate=True turns the event stems (transfers, actions) into a growing
    ledger: new rows are MERGED into the staged file rather than overwriting it, so
    each commit is a superset and the watcher never tombstones prior flow. Asset
    snapshots are always rewritten wholesale (latest quote wins)."""
    os.makedirs(output_dir, exist_ok=True)
    nodes, edges = build_graph(events)
    counts: dict = {}
    written_stems = set()
    for entity, node_list in nodes.items():
        stem = _STEM.get(entity, entity.lower())
        written_stems.add(stem)
        path = os.path.join(output_dir, f"volume_{volume}_{stem}.json")
        if accumulate and stem not in _SNAPSHOT_STEMS:
            merged, added = _merge_keep_order(
                _load_node_list(path), node_list, lambda r: r.get("name"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(merged, f)
            counts[entity] = len(merged)
            print(f"   ✅ {entity:<18}: {len(merged):,} (+{added:,} new) → "
                  f"{os.path.basename(path)}")
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(node_list, f)
            counts[entity] = len(node_list)
            print(f"   ✅ {entity:<18}: {len(node_list):,} → {os.path.basename(path)}")
    epath = os.path.join(output_dir, f"volume_{volume}_edges.json")
    if accumulate:
        merged_e, added_e = _merge_keep_order(
            _load_node_list(epath), edges,
            lambda e: (e.get("rel"), e.get("from"), e.get("to")))
        with open(epath, "w", encoding="utf-8") as f:
            json.dump(merged_e, f)
        counts["edges"] = len(merged_e)
        print(f"   ✅ {'edges':<18}: {len(merged_e):,} (+{added_e:,} new) → "
              f"{os.path.basename(epath)}")
    else:
        with open(epath, "w", encoding="utf-8") as f:
            json.dump(edges, f)
        counts["edges"] = len(edges)
        print(f"   ✅ {'edges':<18}: {len(edges):,} → {os.path.basename(epath)}")
    # Remove stale robinhood type-volumes for this volume that this run didn't write
    # (e.g. a prior --with-transfers left volume_N_transfers.json, now Asset-only) so a
    # commit never picks up stale data. Only touches OUR known stems — never another
    # pipeline's files sharing the dir. Under --accumulate the ledger stems are meant
    # to persist across cycles, so only prune snapshot stems (which never go stale).
    prunable = ({s for s in _STEM.values() if s in _SNAPSHOT_STEMS}
                if accumulate else set(_STEM.values()))
    for stem in prunable - written_stems:
        stale = os.path.join(output_dir, f"volume_{volume}_{stem}.json")
        if os.path.exists(stale):
            os.remove(stale)
            print(f"   🧹 removed stale {os.path.basename(stale)}")
    return counts


# ---------------------------------------------------------------------------
# EMBED-TEXT PREVIEW — mirrors embeddings._embed_and_upload's `auto` composer so
# `--dry-run` shows what the Path A loop would embed, with no fastembed/Qdrant.
# ---------------------------------------------------------------------------
def compose_searchable_text(fields: dict, role_map: dict = ROBINHOOD_ROLE_MAP) -> str:
    tags = " ".join(str(fields.get(t, "")) for t in role_map.get("tags", [])
                    if isinstance(fields.get(t), str))
    subtitle = fields.get(role_map.get("subtitle", ""), "")
    text_terms = "; ".join(str(fields[t]) for t in (role_map.get("text", []) or [])
                           if fields.get(t))
    s = f"Title: {fields.get(role_map.get('title', ''), '')}. Tags: {tags}"
    if subtitle:
        s += f". Subtitle: {subtitle}"
    if text_terms:
        s += f". {text_terms}"
    return f"search_document: {s[:1000]}"


# Robinhood Chain mainnet (Arbitrum-Orbit EVM L2). These are the live defaults;
# override via --rpc-url / --blockscout-url. The Blockscout explorer is the chain's
# own indexer — it is NOT the Fangorn subgraph (that's downstream, populated when we
# publish via `fangorn commit`).
ROBINHOOD_CHAIN_ID = 4663
ROBINHOOD_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_BLOCKSCOUT = "https://robinhoodchain.blockscout.com"

# Tokenized stocks are ERC-20s named "<Company> • Robinhood Token". This marks them
# out from the chain's stablecoins (USDe, USDG) in the token list.
_RH_TOKEN_MARKER = "Robinhood Token"

# Light sector hints — improve the embedded blurb; everything else defaults to
# "equity". Not exhaustive; extend freely (Blockscout carries no sector field).
_SECTORS = {
    "AAPL": "technology", "MSFT": "technology", "GOOGL": "technology",
    "META": "technology", "ORCL": "technology", "PLTR": "technology",
    "NVDA": "semiconductors", "AMD": "semiconductors", "INTC": "semiconductors",
    "MU": "semiconductors", "SNDK": "semiconductors", "CRWV": "semiconductors",
    "TSLA": "automotive", "AMZN": "consumer", "BABA": "consumer",
    "COIN": "crypto", "CRCL": "crypto", "MSTR": "crypto",
    "SPY": "etf", "QQQ": "etf", "SLV": "etf", "USO": "etf",
    "SPCX": "aerospace",
}


def _clean_name(name: str) -> str:
    """"Apple • Robinhood Token" → "Apple"."""
    return name.split("•")[0].strip() or name


def read_robinhood_events(rpc_url: str | None = None, *,
                          blockscout_url: str | None = None, block_gt: int = 0,
                          max_assets: int = 0,
                          with_transfers: bool = False, max_transfers: int = 5) -> list[dict]:
    """Read raw Robinhood-Chain events from the live chain — the tokenized-stock
    universe + live prices from the Blockscout explorer, block height from JSON-RPC
    (and, with `with_transfers`, real on-chain Transfer flow). This is the UPSTREAM
    read — the Robinhood source, NOT a subgraph (the subgraph is downstream, populated
    by `fangorn commit`). Only events with blockNumber > block_gt are returned."""
    evs = _read_robinhood_chain(rpc_url or ROBINHOOD_RPC_URL,
                                blockscout_url or ROBINHOOD_BLOCKSCOUT,
                                max_assets, with_transfers, max_transfers)
    # The floor gates transfer FLOW only. Asset snapshots are live price quotes stamped
    # at chain head, not block-gated events — always emit them so a quote refresh lands
    # every poll (and a floor set above head still yields the universe, just no flow).
    return [e for e in evs
            if e.get("type") != "transfer" or int(e.get("blockNumber", 0) or 0) > block_gt]


def _read_robinhood_chain(rpc_url: str, blockscout_url: str, max_assets: int = 0,
                          with_transfers: bool = False, max_transfers: int = 5) -> list[dict]:
    """Read the live tokenized-stock universe from Robinhood Chain and emit one
    `asset` snapshot per stock (symbol, cleaned name, live exchange_rate as price,
    circulating market cap, holder count, on-chain supply, token address).

    With `with_transfers`, also read each token's recent ERC-20 Transfer events (real
    on-chain flow): the Asset gains `recentVolume` / `recentTransfers` measures, and
    the `max_transfers` largest recent transfers are emitted as their own `transfer`
    events (linked to the Asset by a `hasTransfer` edge) — a second entity type + the
    edges that were otherwise absent. Costs one extra Blockscout call per token.

    The universe + prices come from the chain's Blockscout API (its own indexer);
    the block height comes from JSON-RPC so snapshots carry a real, monotonically
    advancing blockNumber (so a live quote refresh clears the block_gt filter each
    poll and re-embeds the moved price). No subgraph — we PUBLISH to the Fangorn
    subgraph downstream via `fangorn commit`."""
    import urllib.parse
    import urllib.request

    def _get(path: str, params: dict | None = None):
        url = blockscout_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "quickbeam-robinhood"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    # Current chain head (for the snapshot's blockNumber). Best-effort — fall back to
    # a wall-clock surrogate so a refresh still advances past block_gt if RPC is down.
    head = int(time.time())
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": "eth_blockNumber", "params": []}).encode()
        req = urllib.request.Request(rpc_url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": "quickbeam-robinhood"})
        with urllib.request.urlopen(req, timeout=20) as r:
            head = int(json.loads(r.read())["result"], 16)
    except Exception as e:  # noqa: BLE001
        print(f"[robinhood] eth_blockNumber failed ({e}); stamping wall-clock block",
              file=sys.stderr)

    def _enc_cursor(npp: dict) -> dict:
        """Blockscout echoes cursor keys that urlencode would mangle: null-valued keys
        (fiat_value, market_cap) become the literal "None" and Python bools serialize
        capitalized ("False") — both 422 the API. Drop nulls, lowercase bools."""
        out = {}
        for k, v in npp.items():
            if v is None:
                continue
            out[k] = "true" if v is True else "false" if v is False else v
        return out

    # Discover the tokenized stocks via Blockscout's token SEARCH (?q=Robinhood Token),
    # which returns them by NAME regardless of market cap. The plain token list is sorted
    # by market cap and its cursor stalls at the null-market-cap tail — dropping the ~18
    # tokenized stocks that have no market cap yet (NFLX, COST, SOFI, QCOM, …). We still
    # filter client-side on the name marker (search is fuzzy), dedupe by address, and stop
    # on an absent/repeated cursor.
    tokens: list[dict] = []
    seen: set = set()
    params: dict = {"q": _RH_TOKEN_MARKER, "type": "ERC-20"}
    prev_cursor: dict | None = None
    for _ in range(40):  # hard bound
        page = _get("/api/v2/tokens", params)
        for t in page.get("items", []):
            addr = t.get("address_hash")
            if _RH_TOKEN_MARKER in (t.get("name") or "") and addr not in seen:
                seen.add(addr)
                tokens.append(t)
        npp = page.get("next_page_params")
        if not npp or npp == prev_cursor or (max_assets and len(tokens) >= max_assets):
            break
        prev_cursor = npp
        params = {"q": _RH_TOKEN_MARKER, "type": "ERC-20", **_enc_cursor(npp)}
    if max_assets:
        tokens = tokens[:max_assets]

    now = int(time.time())
    out: list[dict] = []
    n_transfers = 0
    for t in tokens:
        sym = t.get("symbol")
        if not sym:
            continue
        name = _clean_name(t.get("name") or sym)
        sector = _SECTORS.get(sym, "equity")
        addr = t.get("address_hash")
        supply_raw = _num(t.get("total_supply"))
        dec = int(t.get("decimals") or 0)
        asset_ev = {
            "type": "asset", "symbol": sym, "name": name, "sector": sector,
            "price": _num(t.get("exchange_rate")),
            "marketCap": _num(t.get("circulating_market_cap")),
            "holders": int(t["holders_count"]) if t.get("holders_count") else None,
            "totalSupply": round(supply_raw / (10 ** dec), 4) if supply_raw and dec else supply_raw,
            "address": addr,
            "blockNumber": head, "blockTimestamp": now,
        }

        # Real on-chain flow: recent ERC-20 Transfer events for this token. Enriches
        # the Asset with volume/count and emits the largest as their own `transfer`
        # events (→ a `hasTransfer` edge). PAGINATED — Blockscout returns ~50 transfers
        # per page, so we walk `next_page_params` (same cursor pattern as token
        # discovery above) until we've collected `max_transfers` or run out of pages.
        # Without this the read is capped at the most recent ~50 no matter how high
        # max_transfers goes, starving the ledger of depth.
        if with_transfers and addr:
            items: list[dict] = []
            # Cap the page walk: enough pages to reach max_transfers (~50/page) plus a
            # little slack, hard-bounded so a stuck cursor can never loop forever.
            max_pages = min(200, max(1, -(-max_transfers // 50) + 2))
            tparams: dict | None = None
            prev_tcursor: dict | None = None
            for _ in range(max_pages):
                try:
                    page = _get(f"/api/v2/tokens/{addr}/transfers", tparams) or {}
                except Exception as e:  # noqa: BLE001 — flow is best-effort, never fatal
                    print(f"[robinhood] transfers for {sym} failed ({e})", file=sys.stderr)
                    break
                items.extend(page.get("items", []))
                npp = page.get("next_page_params")
                if not npp or npp == prev_tcursor or len(items) >= max_transfers:
                    break
                prev_tcursor = npp
                tparams = _enc_cursor(npp)

            def _tokens_moved(it):
                tot = it.get("total") or {}
                v = _num(tot.get("value"))
                d = int(tot.get("decimals") or dec or 18)
                return v / (10 ** d) if v is not None else 0.0

            sized = sorted(((_tokens_moved(it), it) for it in items), key=lambda x: -x[0])
            asset_ev["recentTransfers"] = len(items)
            asset_ev["recentVolume"] = round(sum(v for v, _ in sized), 4)
            out.append(asset_ev)
            px = asset_ev.get("price")             # token→USD at this snapshot
            for v, it in sized[:max_transfers]:
                out.append({
                    "type": "transfer", "symbol": sym, "name": name, "sector": sector,
                    "value": round(v, 4),
                    # USD notional so cross-asset flow sums (aggregate sum:usdValue)
                    # are comparable — raw `value` is in each token's own units.
                    "usdValue": round(v * px, 2) if px else None,
                    "fromAddr": (it.get("from") or {}).get("hash"),
                    "toAddr": (it.get("to") or {}).get("hash"),
                    "txHash": it.get("transaction_hash"),
                    "logIndex": it.get("log_index"),
                    "blockNumber": int(it.get("block_number") or head),
                    # Real on-chain block time from Blockscout (not the read wall-clock)
                    # so the Transfer record carries a genuine, sortable timestamp.
                    "blockTimestamp": _iso_to_epoch(it.get("timestamp")) or now,
                })
                n_transfers += 1
        else:
            out.append(asset_ev)

    extra = f" + {n_transfers} notable transfer(s)" if with_transfers else ""
    print(f"[robinhood] read {len(tokens)} tokenized stock(s){extra} from Robinhood "
          f"Chain (head block {head})")
    return out


# ---------------------------------------------------------------------------
# CLI — `quickbeam data robinhood` (INGEST ONLY)
# ---------------------------------------------------------------------------
def _parse_args():
    p = argparse.ArgumentParser(
        prog="quickbeam data robinhood",
        description="Transofrm Robinhood Chain events into volumes."
                    "Publish with `fangorn commit"
                    "--bundle`, then embed with `quickbeam watch --bundle`.")
    p.add_argument("--rpc-url", default=None,
                   help=f"Override the Robinhood-Chain JSON-RPC URL (default "
                        f"{ROBINHOOD_RPC_URL}, chain id {ROBINHOOD_CHAIN_ID}). NOT a "
                        f"subgraph — that's downstream, populated by `fangorn commit`.")
    p.add_argument("--blockscout-url", default=None,
                   help=f"Override the Blockscout explorer API (default "
                        f"{ROBINHOOD_BLOCKSCOUT}).")
    p.add_argument("--max-assets", type=int, default=0,
                   help="Cap the number of tokenized stocks read (0 = all).")
    p.add_argument("--with-transfers", action="store_true",
                   help="Also read each token's recent on-chain ERC-20 Transfer flow: "
                        "adds recentVolume/recentTransfers to each Asset and emits the "
                        "largest transfers as linked `Transfer` nodes (a 2nd entity "
                        "type + edges). One extra Blockscout call per token.")
    p.add_argument("--max-transfers", type=int, default=5,
                   help="How many recent transfers to collect per token "
                        "(--with-transfers). Now PAGINATED: values above ~50 walk "
                        "Blockscout's transfer pages to reach the target depth (the "
                        "largest by size are emitted as Transfer nodes). Raise this "
                        "(e.g. 500) to capture real flow depth instead of the newest ~50.")
    p.add_argument("--block-gt", type=int, default=0,
                   help="Only read events with blockNumber greater than this (a manual, "
                        "one-shot floor; --start-block/--checkpoint-file are the persisted "
                        "live-tail equivalents).")
    p.add_argument("--start-block", type=int, default=0,
                   help="Block to begin reading transfer flow from: emit only transfers "
                        "with blockNumber > max(START, last-checkpointed block). Asset "
                        "price snapshots (stamped at chain head) are always emitted. This "
                        "is a live floor, not a historical backfill — Blockscout is read "
                        "newest-first, so blocks below the first page aren't paged back "
                        "into.")
    p.add_argument("--checkpoint-file", default=None,
                   help="Persist the highest transfer blockNumber seen to this JSON file "
                        "and resume above it each cycle, so the live tail never re-emits or "
                        "misses across restarts. Combined with --start-block as the floor. "
                        "NOTE: this is the INGEST checkpoint; `watch --checkpoint-file` is a "
                        "separate embed-side cursor.")

    p.add_argument("--accumulate", action="store_true",
                   help="Grow a transfer LEDGER instead of overwriting: merge new "
                        "transfers/edges into the staged files (dedup by id, old rows "
                        "kept) so each fangorn commit is a superset of the last. This is "
                        "what lets the watcher's index grow past the current snapshot — "
                        "without it, every commit replaces the prior flow and the "
                        "watcher's delete-propagation collapses the index back to ~one "
                        "snapshot. Asset quotes are always rewritten (latest wins). Pair "
                        "with --checkpoint-file so a restart resumes the ledger.")

    p.add_argument("--output-dir", default="./stage_volumes",
                   help="Where to write volume_<n>_*.json node/edge files.")
    p.add_argument("--volume", type=int, default=1,
                   help="Volume number (coexists with other pipelines' volumes).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the shaped nodes + their embed text; write nothing.")

    # ── Live ingest daemon ─────────────────────────────────────────────────────
    p.add_argument("--watch", action="store_true",
                   help="Run as a long-lived daemon: re-read the chain every "
                        "--poll-interval seconds and re-emit (and, with --publish, "
                        "re-commit) the snapshot. Also runnable one-shot from cron.")
    p.add_argument("--poll-interval", type=int, default=120,
                   help="Seconds between reads in --watch mode (default 120; chain "
                        "block time is ~101s, so a full snapshot refreshes each cycle).")

    pub = p.add_argument_group("publish to fangorn (optional — the ingest→publish leg)")
    pub.add_argument("--publish", action="store_true",
                     help="After writing volumes, run `fangorn commit --bundle` + "
                          "`fangorn push` to publish the snapshot on-chain. The repo "
                          "must be `fangorn repo init`'d against the bundle schema first.")
    pub.add_argument("--repo", default=".",
                     help="Fangorn repo dir to run commit/push in (its cwd).")
    pub.add_argument("--fangorn-bin", default="fangorn",
                     help="The fangorn CLI invocation — a full command, not just a path "
                          "(shell-split). Default `fangorn` (a global install reading "
                          "~/.fangorn/config.json). For the git-native dev build use its "
                          "wrapper, e.g. \"dotenvx run -f ~/fangorn/fangorn/.env -- node "
                          "~/fangorn/fangorn/dist/src/cli/cli.js\".")
    pub.add_argument("--commit-message", default=None,
                     help="Commit message (default: a timestamped snapshot message).")
    return p.parse_args()


def run() -> None:
    args = _parse_args()

    if args.watch:
        _watch_ingest(args)
    else:
        _ingest_once(args)


def _print_dry_run(events: list[dict]) -> None:
    nodes, edges = build_graph(events)
    for entity, node_list in nodes.items():
        print(f"\n[{entity}] {len(node_list)} node(s)")
        for n in node_list[:4]:
            print(f"  · {n['name']}")
            print(f"    embed → {compose_searchable_text(n['fields'])}")
    print(f"\n[edges] {len(edges)}: " + ", ".join(sorted({e['rel'] for e in edges})))


# The ingest block cursor lives under this key. It is deliberately distinct from the
# watcher's embed-side checkpoint (schema {manifests, processed_track_ids, last_tip,
# last_block, …}) so the two never alias — and the writer MERGES, so even if a user
# points --checkpoint-file at the shared file, foreign keys survive untouched.
_INGEST_BLOCK_KEY = "robinhoodIngestBlock"


def _read_json_obj(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:  # noqa: BLE001 — a bad checkpoint must not stall ingest
        print(f"[robinhood] checkpoint {path!r} unreadable ({e}); starting from floor",
              file=sys.stderr)
        return {}


def _load_ingest_checkpoint(path: str | None) -> int:
    """Highest transfer blockNumber previously ingested (0 if none/unset)."""
    return int(_read_json_obj(path).get(_INGEST_BLOCK_KEY, 0) or 0)


def _save_ingest_checkpoint(path: str | None, block: int) -> None:
    if not path:
        return
    d = _read_json_obj(path)      # merge into whatever is there — never clobber foreign keys
    d[_INGEST_BLOCK_KEY] = int(block)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)  # atomic — a crashed write never truncates the checkpoint


def _ingest_once(args) -> int:
    """One read → emit volumes (→ optionally publish to fangorn). Returns event count.
    This is the unit a cron job runs each tick, and the daemon runs each cycle.

    The effective transfer floor is max(--block-gt, --start-block, checkpoint) so the
    live tail resumes above the last block seen and never re-emits it."""
    checkpoint = _load_ingest_checkpoint(args.checkpoint_file)
    floor = max(args.block_gt, args.start_block, checkpoint)
    events = read_robinhood_events(
        args.rpc_url, blockscout_url=args.blockscout_url,
        block_gt=floor, max_assets=args.max_assets,
        with_transfers=args.with_transfers, max_transfers=args.max_transfers)
    if not events:
        print("[robinhood] no events to ingest")
        return 0
    if args.dry_run:
        _print_dry_run(events)
        return len(events)
    print(f"[robinhood] ingesting {len(events)} event(s) → {args.output_dir} "
          f"(volume {args.volume})")
    emit_volumes(events, args.output_dir, args.volume, accumulate=args.accumulate)
    # Advance the checkpoint to the highest transfer block actually staged this cycle
    # (asset snapshots are stamped at chain head, so exclude them or the floor would
    # jump to head and drop transfers that land in lower blocks next cycle). After
    # emit_volumes so --dry-run stays side-effect-free.
    tx_blocks = [int(e.get("blockNumber", 0) or 0)
                 for e in events if e.get("type") == "transfer"]
    if tx_blocks and max(tx_blocks) > checkpoint:
        _save_ingest_checkpoint(args.checkpoint_file, max(tx_blocks))
    if args.publish:
        _publish_to_fangorn(args)
    else:
        print("[robinhood] next: `quickbeam data schemagen` → `fangorn commit "
              "--bundle` → `quickbeam watch --bundle …`  (or add --publish to commit now)")
    return len(events)


def _watch_ingest(args) -> None:
    """Long-lived ingest daemon: re-read the chain every --poll-interval seconds and
    re-emit (and, with --publish, re-commit) the current snapshot. Because tokenized
    prices/holders move, each cycle is a fresh full snapshot; fangorn's structural
    sharing means a re-commit only re-uploads the tokens that actually changed."""
    floor = max(args.block_gt, args.start_block, _load_ingest_checkpoint(args.checkpoint_file))
    print(f"[robinhood] ingest daemon — source=live-chain, out={args.output_dir} "
          f"(volume {args.volume}), publish={args.publish}, poll={args.poll_interval}s, "
          f"transfer floor=block>{floor}, mode={'ledger (accumulate)' if args.accumulate else 'snapshot (replace)'}"
          + (f" (checkpoint {args.checkpoint_file})" if args.checkpoint_file else ""))
    cycle = 0
    while True:
        cycle += 1
        print(f"\n[robinhood] ── ingest cycle {cycle} ──")
        try:
            _ingest_once(args)
        except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the daemon
            print(f"[robinhood] cycle {cycle} error: {e}", file=sys.stderr)
        print(f"[robinhood] sleeping {args.poll_interval}s…")
        time.sleep(args.poll_interval)


def _publish_to_fangorn(args) -> bool:
    """Publish the just-written volumes on-chain: `fangorn commit --bundle` + `fangorn
    push`, run in the repo dir. This is the ingest→publish leg — the on-chain write
    that emits the DataSource events a subgraph indexes and `watch --bundle` reads."""
    import datetime
    import shlex
    import subprocess
    # Preflight: commit --bundle only works inside an initialized repo. Fail clearly
    # once instead of a confusing non-zero exit every cycle.
    if not os.path.isdir(os.path.join(args.repo, ".fangorn")):
        print(f"[robinhood] --publish: {args.repo!r} is not a fangorn repo (no .fangorn/). "
              f"Bootstrap once: cd there and `fangorn repo init <name> -s <bundleSchema>`. "
              f"Skipping publish.", file=sys.stderr)
        return False
    msg = args.commit_message or (
        "robinhood snapshot "
        + datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    # --fangorn-bin may be a full command, not just an executable — e.g. the dev
    # invocation `dotenvx run -f /abs/.env -- node /abs/dist/src/cli/cli.js`. Split it
    # so the subcommand/args append cleanly. (.env is loaded by that wrapper, not us;
    # a globally-installed `fangorn` reads ~/.fangorn/config.json instead.)
    prefix = shlex.split(args.fangorn_bin)
    steps = [
        ("commit", [*prefix, "commit", "--bundle",
                    os.path.abspath(args.output_dir), "--volume", str(args.volume),
                    "-m", msg]),
        ("push", [*prefix, "push"]),
    ]
    for label, cmd in steps:
        print(f"[robinhood] fangorn {label} (cwd={args.repo}): {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, cwd=args.repo)
        except FileNotFoundError:
            print(f"[robinhood] fangorn CLI not found (--fangorn-bin {args.fangorn_bin!r}, "
                  f"resolved to {prefix[0]!r}). Install the git-native fangorn or pass its "
                  f"full invocation, e.g. --fangorn-bin \"dotenvx run -f "
                  f"~/fangorn/fangorn/.env -- node ~/fangorn/fangorn/dist/src/cli/cli.js\". "
                  f"Skipping publish.", file=sys.stderr)
            return False
        if r.returncode != 0:
            print(f"[robinhood] fangorn {label} failed (exit {r.returncode})",
                  file=sys.stderr)
            return False
    print("[robinhood] published snapshot to fangorn ✓")
    return True


if __name__ == "__main__":
    run()
