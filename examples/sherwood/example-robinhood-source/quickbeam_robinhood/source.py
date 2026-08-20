"""
A Robinhood-Chain `Source` for the quickbeam ingestion harness.

This is an EXAMPLE of the pluggable-source pattern: quickbeam core ships no
concrete sources, so a data source lives in its own package (this one) and
registers a `quickbeam.sources` entry point (see ../pyproject.toml). Installing
this package makes `quickbeam data robinhood` work with the full harness loop —
CLI, staged-volume emission, checkpointing, the `--watch` daemon, and `--publish`
to fangorn — with zero changes to quickbeam.

WHAT A SOURCE SUPPLIES
----------------------
Only read + shape + cursor. Everything generic is the harness's job:

  read(cursor, args)      → raw event dicts from the upstream (the live chain)
  build_graph(records)    → ({entityType: [{name, fields}]}, [edge]) — PURE, testable
  next_cursor(records, p) → the checkpoint to persist (max transfer block seen)

The harness turns `build_graph`'s output into staged `volume_<n>_*.json` files and,
under `--publish`, assembles them into one `fangorn upload` batch written into the
configured wallet's namespace. `quickbeam watch --source <owner>:robinhood` reads
that namespace back, embeds it, and ships CDN deltas.

READING ROBINHOOD DATA
----------------------
The upstream source is Robinhood Chain mainnet (id 4663): the tokenized-stock
universe + live prices come from its Blockscout explorer API (the chain's own
indexer), block height from JSON-RPC, and — with `--with-transfers` — real on-chain
ERC-20 Transfer flow. There is no fixture mode; every read hits the live chain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# ROLE MAP + PRESENTATION — the field roles the harness `--dry-run` preview uses
# to show what the embed loop would index. The high-value semantic signal is the
# verbalized `text` blurb; price/deviation are structured measures for hybrid
# filtering, not embedded prose.
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
        "Transfer":           "swap_horiz",
        "Wallet":             "account_balance_wallet",
        "CorporateAction":    "account_balance",
        "OracleUpdate":       "bolt",
        "LiquidityRebalance": "water_drop",
        "NewsSentiment":      "newspaper",
    },
}

# Event type → (entityType, edge relation from its Asset). `transfer` is real
# on-chain flow (ERC-20 Transfer events). The other event types are scaffolding for
# off-chain sibling feeds (corporate actions, news sentiment) — none exist on-chain
# today, but the shaper understands them so the graph is ready when a feed is added.
_EVENT_SPEC = {
    "corporate_action":    ("CorporateAction",    "hasAction"),
    "oracle_update":       ("OracleUpdate",       "hasOracleUpdate"),
    "liquidity_rebalance": ("LiquidityRebalance", "hasLiquidity"),
    "news_sentiment":      ("NewsSentiment",       "hasNews"),
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
    """Blockscout ISO-8601 timestamp → epoch seconds, or None if unparseable.
    Blockscout stamps each transfer with the real on-chain block time — this is
    what lets downstream sequence flow (holding periods, before/after splits)."""
    if not s:
        return None
    try:
        import datetime as _dt
        return int(_dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _epoch_to_utc(ts):
    """Epoch seconds → "YYYY-MM-DD HH:MM UTC", or None. Used to fold a transfer's
    REAL on-chain block time into its embedded blurb. Deterministic: the time comes
    from the event itself (not wall-clock), so the same transfer verbalizes identically."""
    if not ts:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _fmt_usd(x):
    """USD with ADAPTIVE precision. These are fractional-share tokens, so flow is
    often sub-dollar; fixed whole-dollar formatting crushes a real $0.17 move to "$0",
    which an embedding reads as a null event. Show cents when small, drop them when big."""
    x = _num(x)
    if x is None:
        return None
    if x == 0:
        return "$0"
    if abs(x) < 0.01:
        return "<$0.01"
    return f"${x:,.0f}" if abs(x) >= 1000 else f"${x:,.2f}"


def _fmt_amt(v):
    """Token amount with adaptive precision — 18-decimal tokenized shares mean a real
    transfer can be 0.0003 units; a fixed 2dp "0.00" hides it. Scale decimals to size."""
    v = _num(v, 0.0)
    if v == 0:
        return "0"
    if abs(v) >= 100:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# FLOW METRICS — the robust, expensive-to-fake shape of a token's on-chain flow.
#
# THE PROBLEM these solve: raw gross counts (recentTransfers, recentVolume) are the
# CHEAPEST thing to fake. A fast-relay ring inflates gross volume with round-trips that
# net to nothing; a 12h cron keeps recentTransfers > 0 forever. If those raw numbers
# drive the embedded blurb / the "active" signal, a semantic search for "liquid, actively
# traded" retrieves washed tokens — the vector space inherits the manipulation.
#
# Each measure below is a dimensionless ratio computed on the transfer WINDOW we actually
# read (honest about its sample), and each targets a specific cheap trick:
#   circularityRatio  — round-trip wash (net displacement ≪ gross)          → fast-relay ring
#   senderHHI         — one wallet driving most volume                      → sybil / single-actor
#   amountQuantization— many transfers sharing one fixed parcel size        → mechanical wash
#   interArrivalCV    — near-constant gaps between transfers (a heartbeat)   → cron metronome
# These go into indexed payload (a `where` clause), NOT the embedding prose. What DOES go
# in the prose is a robust description of TRUST (concentrated/circular vs broad/organic).
# ---------------------------------------------------------------------------
def _flow_metrics(transfers: list[dict]) -> dict:
    """Robust flow signals from a window of transfers. Each transfer is
    {"value": <token units float>, "from": <addr|None>, "to": <addr|None>, "ts": <epoch|None>}.
    Returns only the measures the sample can honestly support (keys absent otherwise), plus
    a composite `manipulationScore` ∈ [0,1] and a coarse `dataQuality` facet. PURE — no I/O,
    so a unit test hand-builds transfers and asserts on the ratios."""
    n = len(transfers)
    if n == 0:
        return {}
    vals = [abs(_num(t.get("value"), 0.0)) for t in transfers]
    gross = sum(vals)

    # Net displacement: every unit that leaves one wallet arrives at another, so summed
    # signed balance deltas are conservative; half their absolute sum is what actually
    # RELOCATED. Round-trips cancel here — gross stays high, net collapses.
    delta: dict[str, float] = {}
    sends: dict[str, float] = {}
    senders: set[str] = set()
    receivers: set[str] = set()
    for t, v in zip(transfers, vals):
        frm, to = t.get("from"), t.get("to")
        if frm:
            delta[frm] = delta.get(frm, 0.0) - v
            sends[frm] = sends.get(frm, 0.0) + v
            senders.add(frm)
        if to:
            delta[to] = delta.get(to, 0.0) + v
            receivers.add(to)
    net = 0.5 * sum(abs(d) for d in delta.values())

    m: dict = {"sampleSize": n}
    if gross > 0:
        # 1.0 = pure wash (all volume round-trips out); 0.0 = pure one-way displacement.
        m["circularityRatio"] = round(max(0.0, min(1.0, 1.0 - net / gross)), 4)
        m["netVolume"] = round(net, 4)
    m["uniqueSenders"] = len(senders)
    m["uniqueReceivers"] = len(receivers)
    m["distinctCounterparties"] = len(senders | receivers)

    # Herfindahl concentration of SEND volume: 1.0 when one wallet sends everything,
    # → 1/k when k senders share equally. Catches a sybil hub the gross count can't.
    total_send = sum(sends.values())
    if total_send > 0:
        m["senderHHI"] = round(sum((s / total_send) ** 2 for s in sends.values()), 4)

    # Quantization: share of transfers tying the single most common (rounded) value. A
    # mechanical wash reuses one parcel size, so this spikes; organic sizes are diffuse.
    from collections import Counter
    quant = Counter(round(v, 8) for v in vals if v > 0)
    if quant:
        m["amountQuantization"] = round(max(quant.values()) / sum(quant.values()), 4)

    # Inter-arrival CV: std/mean of gaps between consecutive transfer times. A cron
    # heartbeat is near-constant (CV → 0); organic flow is bursty/Poisson (CV ≳ 1). Needs
    # ≥ 3 timestamps to have ≥ 2 gaps with a meaningful spread.
    ts = sorted(t["ts"] for t in transfers if t.get("ts") is not None)
    if len(ts) >= 3:
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        mean = sum(gaps) / len(gaps)
        if mean > 0:
            var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
            m["interArrivalCV"] = round((var ** 0.5) / mean, 4)

    # Composite manipulation score — a weighted blend of the signals present (renormalized
    # so a missing metric doesn't drag the score to 0). High circularity, high sender
    # concentration, high quantization, and LOW arrival variance all push it up.
    terms: list[tuple[float, float]] = []  # (weight, value∈[0,1])
    if "circularityRatio" in m:
        terms.append((0.40, m["circularityRatio"]))
    if "senderHHI" in m:
        terms.append((0.30, m["senderHHI"]))
    if "amountQuantization" in m:
        terms.append((0.20, m["amountQuantization"]))
    if "interArrivalCV" in m:
        terms.append((0.10, max(0.0, 1.0 - min(1.0, m["interArrivalCV"]))))  # regularity
    if terms:
        wsum = sum(w for w, _ in terms)
        m["manipulationScore"] = round(sum(w * v for w, v in terms) / wsum, 4)

    # Coarse, filterable trust facet derived from the score + sample size. Deliberately
    # conservative: a tiny sample can't be called organic, only "sparse".
    score = m.get("manipulationScore")
    if n < 3 or score is None:
        m["dataQuality"] = "sparse"
    elif score >= 0.66:
        m["dataQuality"] = "suspect"
    elif score >= 0.33:
        m["dataQuality"] = "mixed"
    else:
        m["dataQuality"] = "organic"
    return m


def _holder_metrics(active_balances: list[float], holders_count: int | None,
                    hit_threshold_wall: bool) -> dict:
    """Real-ownership shape from the NON-DUST holders (balances already filtered above a
    dust threshold and sorted descending). `holders_count` is Blockscout's raw holder
    total (dust included). PURE. Returns:
      activeHolders    — count of holders above the dust threshold that we saw
      dustHolderShare  — 1 - activeHolders/holders_count (the 4,203-"holder" tokens that
                         are mostly dust recipients light up near 1.0)
      topHolderShare   — largest active holder's share of the active balance (whale grip)
    `hit_threshold_wall` True means we stopped at the dust boundary, so `activeHolders` is
    the true count; False means we stopped on a page cap and it's a lower bound (flagged)."""
    m: dict = {}
    active = len(active_balances)
    m["activeHolders"] = active
    if not hit_threshold_wall:
        m["activeHoldersIsLowerBound"] = True
    if holders_count and holders_count > 0:
        m["dustHolderShare"] = round(max(0.0, 1.0 - active / holders_count), 4)
    tot = sum(active_balances)
    if tot > 0:
        m["topHolderShare"] = round(max(active_balances) / tot, 4)
    return m


# ---------------------------------------------------------------------------
# BUSINESS PROFILES — one factual sentence per ticker describing WHAT the company
# does. Blockscout gives us price/holders/market-cap but no description, so every
# Asset would otherwise embed the same "<name> is a tokenized <sector> stock"
# boilerplate — collapsing all vectors into one cluster (undiscriminating search).
# Folding this line into the embedded text is what lets "AI chip makers", "quantum
# computing", "space & satellites" etc. actually retrieve the right names. Curated
# & deterministic, keyed by symbol; new listings fall through to the stat-line.
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


def _flow_prose(ev: dict) -> str:
    """Describe a token's on-chain flow as TRUST, not counts — this is the ONLY activity
    text that reaches the embedding. The gameable raw numbers (recentTransfers/recentVolume)
    are deliberately kept OUT: a wash ring can inflate gross volume 7× but cannot make its
    flow read as 'broadly distributed / organic', because that verdict comes from the robust
    metrics (circularity, senderHHI, arrival regularity — see _flow_metrics). No exact counts
    appear here on purpose; those live in the indexed payload for a `where` clause."""
    dq = ev.get("dataQuality")
    n, rt = ev.get("sampleSize"), ev.get("recentTransfers")
    if dq is None and n is None and rt is None:
        return ""                                  # no flow leg ran — stay purely descriptive
    if n == 0 or rt == 0:
        return " Quiet: no recent on-chain flow."
    if dq == "suspect":
        return (" On-chain flow looks manipulated — largely circular and concentrated in a "
                "few wallets, so its raw trading volume is not trustworthy.")
    if dq == "mixed":
        return (" On-chain flow shows some concentration or circularity — treat its trading "
                "volume with caution.")
    if dq == "organic":
        return " Actively and organically traded — flow is broadly distributed across many wallets."
    return " Thinly traded on-chain — too few recent transfers to characterize its flow."


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
        # On-chain ACTIVITY as TRUST, not counts. Raw recentTransfers/recentVolume are the
        # cheapest thing to fake (a relay ring or a cron trivially moves them), so they are
        # kept OUT of the embedded text; what goes in is the robust character of the flow.
        parts.append(_flow_prose(ev))
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
        # USD notional + the REAL block time make the blurb self-describing: size lets
        # "large flow" retrieve whales, the date grounds "recent" without relying on
        # read-time. Both come from the event, so the text stays deterministic.
        usd = _fmt_usd(ev.get("usdValue"))
        usd_str = f" (~{usd})" if usd and usd != "$0" else ""
        when = _epoch_to_utc(ev.get("blockTimestamp"))
        when_str = f" on {when}" if when else ""
        return (f"On-chain transfer of {_fmt_amt(ev.get('value'))} {sym}{usd_str} "
                f"({name}) from {frm}… to {to}…{when_str}.")
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
        if dc is not None:
            return "gainer" if _num(dc, 0) >= 0 else "loser"
        # Prefer the ROBUST trust facet over the gameable raw count: `active` used to be
        # just recentTransfers > 0, which a cron satisfies forever. dataQuality is derived
        # from circularity/HHI/arrival-regularity, so a wash ring reads as `wash-suspect`.
        dq = ev.get("dataQuality")
        if dq is not None:
            return {"organic": "active-organic", "mixed": "active-mixed",
                    "suspect": "wash-suspect", "sparse": "thin"}.get(dq, dq)
        rt = ev.get("recentTransfers")   # fallback: flow read without metrics
        if rt is not None:
            return "active" if int(_num(rt, 0)) > 0 else "quiet"
        return "listed"                  # bare snapshot, no flow read
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
    """Raw event → node fields. Carries the verbalized `text` blurb (embedded), the
    ticker/sector/signal facets, and the type-specific structured measures (indexed
    for hybrid filtering)."""
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
              "value", "usdValue", "recentVolume", "recentVolumeUsd",
              "recentTransfers", "lastActivityBlock", "lastActivityAt", "observedAt",
              # ROBUST FLOW METRICS — indexed as filterable measures (a `where` clause),
              # deliberately NOT folded into the embedded blurb: numbers belong in a
              # filter, trust belongs in the prose. See _flow_metrics / verbalize.
              "netVolume", "netVolumeUsd", "circularityRatio", "senderHHI",
              "uniqueSenders", "uniqueReceivers", "distinctCounterparties",
              "amountQuantization", "interArrivalCV", "manipulationScore", "sampleSize",
              # HOLDER SHAPE (with --with-holders).
              "activeHolders", "dustHolderShare", "topHolderShare"):
        if ev.get(k) is not None:
            fields[k] = _num(ev[k])
    for k in ("observedAt", "lastActivityAt", "lastActivityBlock", "recentTransfers",
              "uniqueSenders", "uniqueReceivers", "distinctCounterparties", "sampleSize",
              "activeHolders"):
        if k in fields:                            # counts/epochs read cleaner as ints
            fields[k] = int(fields[k])
    for k in ("dataQuality",):                     # coarse string trust facet
        if ev.get(k) is not None:
            fields[k] = ev[k]
    if ev.get("activeHoldersIsLowerBound"):
        fields["activeHoldersIsLowerBound"] = True
    if ev.get("address"):
        fields["address"] = ev["address"]         # on-chain token contract
    for k in ("fromAddr", "toAddr", "txHash"):     # transfer provenance
        if ev.get(k):
            fields[k] = ev[k]
    # TIME — the honest model: only DISCRETE events (transfers) carry a real event
    # time (blockNumber + block timestamp), so only they get `blockNumber`/`timestamp`
    # to SEQUENCE flow (holding periods, turnover). An Asset is a LIVE quote with no
    # event time of its own — stamping it with read-time wall-clock would make every
    # quote read as "happened now". Instead an Asset carries `observedAt` (WHEN we read
    # it — indexed as staleness metadata, NOT an event time, and NOT in the blurb) and,
    # with flow, `lastActivityAt`/`lastActivityBlock` — the real chain time of its most
    # recent transfer, a true freshness/liveness anchor. All handled via the loop above.
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


# ---------------------------------------------------------------------------
# GRAPH — a batch of events → typed nodes + edges (the harness data contract).
#
# One Asset node per symbol (latest snapshot wins; a bare {symbol,name,sector}
# node is synthesized for symbols seen only through events). Each discrete event
# is its own node, linked from its Asset by a typed edge; transfers additionally
# link to first-class Wallet nodes for their sender/receiver.
# ---------------------------------------------------------------------------
def build_graph(events: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Return ({entityType: [{"name", "fields"}]}, [edge...]). PURE — no I/O, so a
    unit test is: hand-build events, call it, assert on the nodes/edges."""
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
        """Promote a bare from/to address string into a first-class Wallet node. The
        id is LOWERCASED (Blockscout returns checksummed hashes — a raw-case id would
        fragment one wallet into several). Deduped by that id. Once a Transfer links
        to its wallets, `Wallet → Transfer → Asset` is walkable via hasTransfer."""
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
        # Wallet endpoints: a Transfer points at its sender/receiver. Only transfers
        # carry from/to addresses.
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


# ---------------------------------------------------------------------------
# LIVE READER — Robinhood Chain mainnet (Arbitrum-Orbit EVM L2). These are the live
# defaults; override via --rpc-url / --blockscout-url. The Blockscout explorer is
# the chain's own indexer — NOT a Fangorn subgraph (that's downstream, populated
# when we publish via `fangorn upload`).
# ---------------------------------------------------------------------------
ROBINHOOD_CHAIN_ID = 4663
ROBINHOOD_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_BLOCKSCOUT = "https://robinhoodchain.blockscout.com"

# Tokenized stocks are ERC-20s named "<Company> • Robinhood Token".
_RH_TOKEN_MARKER = "Robinhood Token"

# Blockscout + the RPC sit behind Cloudflare, which hands out AAAA (IPv6) records
# whose return path can black-hole — connecting there leaves the socket ESTAB and the
# read blocked in poll() well past its nominal timeout (the 5-minute "hang"). It also
# 500s intermittently. `_http_json` neutralizes both: IPv4 is pinned for the duration
# of the call so a dead IPv6 path can't be chosen, and transient failures (5xx / socket
# errors / timeouts) get a short bounded retry. One flaky call can no longer freeze or
# kill an ingest cycle.
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, *args, **kwargs):
    return _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)


def _http_json(req: urllib.request.Request, *, timeout: float = 20.0,
               retries: int = 2, backoff: float = 1.5):
    """urlopen → parsed JSON, with IPv4 pinned, a hard per-attempt timeout, and a
    short retry on transient upstream failures (HTTP 5xx / connection errors /
    timeouts). 4xx and the final attempt raise; the caller decides fatal vs. skip."""
    last: Exception | None = None
    socket.getaddrinfo = _getaddrinfo_ipv4
    try:
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                last = e
                if e.code < 500 or attempt == retries:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if attempt == retries:
                    raise
            time.sleep(backoff * (attempt + 1))
    finally:
        socket.getaddrinfo = _orig_getaddrinfo
    raise last  # unreachable (loop either returns or raises), satisfies type-checkers

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
                          with_transfers: bool = False, max_transfers: int = 5,
                          with_holders: bool = False) -> list[dict]:
    """Read raw Robinhood-Chain events from the live chain — the tokenized-stock
    universe + live prices from the Blockscout explorer, block height from JSON-RPC
    (and, with `with_transfers`, real on-chain Transfer flow). Only transfers with
    blockNumber > block_gt are returned; Asset snapshots are always emitted (they are
    live price quotes stamped at chain head, not block-gated events)."""
    evs = _read_robinhood_chain(rpc_url or ROBINHOOD_RPC_URL,
                                blockscout_url or ROBINHOOD_BLOCKSCOUT,
                                max_assets, with_transfers, max_transfers, with_holders)
    return [e for e in evs
            if e.get("type") != "transfer" or int(e.get("blockNumber", 0) or 0) > block_gt]


def _read_robinhood_chain(rpc_url: str, blockscout_url: str, max_assets: int = 0,
                          with_transfers: bool = False, max_transfers: int = 5,
                          with_holders: bool = False) -> list[dict]:
    """Read the live tokenized-stock universe from Robinhood Chain and emit one
    `asset` snapshot per stock. With `with_transfers`, also read each token's recent
    ERC-20 Transfer events: the Asset gains recentVolume/recentTransfers and the
    `max_transfers` largest are emitted as their own `transfer` events (linked by a
    hasTransfer edge)."""
    def _get(path: str, params: dict | None = None):
        url = blockscout_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "quickbeam-robinhood"})
        # IPv4-pinned + bounded timeout + retry-on-5xx (see _http_json). A transient
        # Blockscout 500 or a black-holed IPv6 path no longer kills the cycle here.
        return _http_json(req, timeout=20.0)

    # Current chain head (for the snapshot's blockNumber). Best-effort — fall back to
    # a wall-clock surrogate so a refresh still advances past block_gt if RPC is down.
    head = int(time.time())
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": "eth_blockNumber", "params": []}).encode()
        req = urllib.request.Request(rpc_url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": "quickbeam-robinhood"})
        head = int(_http_json(req, timeout=20.0)["result"], 16)
    except Exception as e:  # noqa: BLE001
        print(f"[robinhood] eth_blockNumber failed ({e}); stamping wall-clock block",
              file=sys.stderr)

    def _enc_cursor(npp: dict) -> dict:
        """Blockscout echoes cursor keys urlencode would mangle: null-valued keys
        become the literal "None" and Python bools serialize capitalized — both 422
        the API. Drop nulls, lowercase bools."""
        out = {}
        for k, v in npp.items():
            if v is None:
                continue
            out[k] = "true" if v is True else "false" if v is False else v
        return out

    # Discover the tokenized stocks via Blockscout's token SEARCH (?q=Robinhood Token),
    # which returns them by NAME regardless of market cap (the plain market-cap-sorted
    # list stalls at the null-mcap tail, dropping NFLX/COST/SOFI/…). Filter client-side
    # on the name marker, dedupe by address, stop on an absent/repeated cursor.
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
    # With --with-transfers each token drives its own paginated Transfer read, so the
    # loop below is the slow leg. Announce the count up front, then log per token, so a
    # live cycle is visibly advancing rather than silent-until-done.
    if with_transfers:
        print(f"[robinhood] discovered {len(tokens)} token(s); reading transfers…",
              file=sys.stderr)

    now = int(time.time())
    out: list[dict] = []
    n_transfers = 0
    n_tokens = len(tokens)
    for i, t in enumerate(tokens, 1):
        sym = t.get("symbol")
        if not sym:
            continue
        if with_transfers:
            print(f"[robinhood]   [{i}/{n_tokens}] {sym}…", file=sys.stderr)
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
            # Observation metadata: WHEN we read this live quote. Indexed for staleness
            # only — deliberately NOT an event `blockTimestamp` (that made every quote
            # read as "happened now") and NOT folded into the embedded blurb.
            "observedAt": now,
            # The chain head at read time. Carried on the RAW event only (shape_fields
            # doesn't pick it up, so it never reaches the embedding); it exists so
            # `freshness_report` can compute head-lag PURELY from the records, with no
            # extra RPC call. All assets from one read share the same head.
            "observedHead": head,
        }

        # REAL OWNERSHIP SHAPE — bounded holder read (opt-in; one extra call leg). Raw
        # `holders` counts every dust recipient, so a token sprayed to thousands of empty
        # wallets looks "widely held". Holders come value-DESCENDING, so we page only until
        # balances cross a ~$1 dust line (or run out) — cheap because the dust tail, however
        # long, is never fetched. Yields activeHolders/dustHolderShare/topHolderShare.
        if with_holders and addr:
            thr_tokens = (1.0 / px) if (px := asset_ev.get("price")) else 0.01
            active_bal: list[float] = []
            hit_wall = False
            hparams: dict | None = None
            prev_hcursor: dict | None = None
            for _ in range(6):  # bounded: we cross the dust line within a page or two
                try:
                    page = _get(f"/api/v2/tokens/{addr}/holders", hparams) or {}
                except Exception as e:  # noqa: BLE001 — best-effort, never fatal
                    print(f"[robinhood] holders for {sym} failed ({e})", file=sys.stderr)
                    break
                stop = False
                for h in page.get("items", []):
                    bal = _num(h.get("value"), 0.0) / (10 ** dec)
                    if bal < thr_tokens:
                        stop = True
                        break
                    active_bal.append(bal)
                npp = page.get("next_page_params")
                if stop or not npp or npp == prev_hcursor:
                    hit_wall = True   # crossed the dust line, or exhausted holders
                    break
                prev_hcursor = npp
                hparams = _enc_cursor(npp)
            asset_ev.update(_holder_metrics(active_bal, asset_ev.get("holders"), hit_wall))

        # Real on-chain flow: recent ERC-20 Transfer events for this token. PAGINATED —
        # Blockscout returns ~50/page, so we walk `next_page_params` until we've
        # collected max_transfers or run out of pages.
        if with_transfers and addr:
            items: list[dict] = []
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
            px = asset_ev.get("price")             # token→USD at this snapshot
            asset_ev["recentTransfers"] = len(items)
            asset_ev["recentVolume"] = round(sum(v for v, _ in sized), 4)
            if px:
                asset_ev["recentVolumeUsd"] = round(asset_ev["recentVolume"] * px, 2)
            # ROBUST FLOW METRICS — computed from the SAME `items` already in hand (zero
            # extra calls): net vs gross, sender concentration, quantization, arrival
            # regularity + a composite manipulationScore. These are what neutralize wash
            # trading; the raw recentVolume/recentTransfers above stay as filterable
            # measures but no longer drive the embedded blurb (see verbalize/_signal).
            flow = [{"value": v,
                     "from": (it.get("from") or {}).get("hash"),
                     "to": (it.get("to") or {}).get("hash"),
                     "ts": _iso_to_epoch(it.get("timestamp"))}
                    for v, it in sized]
            fm = _flow_metrics(flow)
            if px and "netVolume" in fm:
                fm["netVolumeUsd"] = round(fm["netVolume"] * px, 2)
            asset_ev.update(fm)
            # Real chain-time freshness anchor for the Asset: the block/time of its most
            # recent transfer. Grounded in on-chain activity, not our read clock.
            blocks = [int(it.get("block_number") or 0) for it in items]
            atimes = [x for x in (_iso_to_epoch(it.get("timestamp")) for it in items) if x]
            if blocks:
                asset_ev["lastActivityBlock"] = max(blocks)
            if atimes:
                asset_ev["lastActivityAt"] = max(atimes)
            out.append(asset_ev)
            for v, it in sized[:max_transfers]:
                out.append({
                    "type": "transfer", "symbol": sym, "name": name, "sector": sector,
                    # 8dp, not 4: these are 18-decimal fractional-share tokens, so a real
                    # transfer can be sub-0.0001 units — 4dp would zero it out.
                    "value": round(v, 8),
                    # USD notional so cross-asset flow sums are comparable — raw
                    # `value` is in each token's own units.
                    "usdValue": round(v * px, 2) if px else None,
                    "fromAddr": (it.get("from") or {}).get("hash"),
                    "toAddr": (it.get("to") or {}).get("hash"),
                    "txHash": it.get("transaction_hash"),
                    "logIndex": it.get("log_index"),
                    "blockNumber": int(it.get("block_number") or head),
                    # Real on-chain block time from Blockscout. If Blockscout ever omits
                    # it, leave it None (honestly absent) rather than fake a read-clock
                    # time — the shaper/blurb simply drop the time for that transfer.
                    "blockTimestamp": _iso_to_epoch(it.get("timestamp")),
                })
                n_transfers += 1
        else:
            out.append(asset_ev)

    extra = f" + {n_transfers} notable transfer(s)" if with_transfers else ""
    print(f"[robinhood] read {len(tokens)} tokenized stock(s){extra} from Robinhood "
          f"Chain (head block {head})")
    return out


# ---------------------------------------------------------------------------
# FRESHNESS — "where does the live tail sit in time?" A per-cycle answer to the
# two DISTINCT staleness questions, computed PURELY from the events just read (the
# head is carried on each asset as `observedHead`, so this needs no extra RPC):
#
#   LAG (am I current?)      head − newest tracked event. This source reads
#                            newest-first, so lag is bounded by --poll-interval, not
#                            a growing backlog. Reported in blocks AND estimated
#                            wall-time (seconds/block is self-calibrated from the
#                            block↔timestamp pairs in the sample — no magic constant).
#   COVERAGE (how much did   the block/time span of the transfers emitted THIS cycle,
#   I pull this cycle?)      plus a per-asset last-activity age histogram — this is
#                            what surfaces the count-window's uneven temporal reach.
#
# NOTE on "lag to head": we track only ~98 specific tokens, so head-lag counts ALL
# other chain blocks too — it OVERSTATES our staleness. The honest liveness number is
# `newestTransferAgeSec` (time since our newest TRACKED event). Both are reported,
# labeled, so neither is mistaken for the other.
# ---------------------------------------------------------------------------
def _age_str(sec) -> str:
    """Compact human age: 45s / 12m / 3.4h / 2.1d. None → "?"."""
    if sec is None:
        return "?"
    sec = int(sec)
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec / 60:.0f}m"
    if sec < 172800:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"


def freshness_report(records: list[dict], cursor: int = 0) -> dict | None:
    """Per-cycle freshness/lag summary — PURE (no I/O), computed from the events just
    read. Returns a dict of structured metrics plus a `display` list of pre-rendered
    lines (the harness prints those and persists the rest under `<name>Freshness`), or
    None when there is nothing to report."""
    assets = [e for e in records if e.get("type") == "asset"]
    txs = [e for e in records if e.get("type") == "transfer"]
    if not assets and not txs:
        return None
    now = int(time.time())
    head = max((int(a["observedHead"]) for a in assets if a.get("observedHead")),
               default=None)

    # (block, time) samples for self-calibrating seconds/block. Assets carry their
    # newest transfer as lastActivityBlock/lastActivityAt (always present, even when a
    # token had nothing NEW past the checkpoint); emitted transfers add finer samples.
    pairs = [(int(t["blockNumber"]), int(t["blockTimestamp"]))
             for t in txs if t.get("blockNumber") and t.get("blockTimestamp")]
    pairs += [(int(a["lastActivityBlock"]), int(a["lastActivityAt"]))
              for a in assets if a.get("lastActivityBlock") and a.get("lastActivityAt")]
    sec_per_block = None
    if len(pairs) >= 2:
        pairs.sort()
        (b0, t0), (b1, t1) = pairs[0], pairs[-1]
        if b1 > b0:
            sec_per_block = (t1 - t0) / (b1 - b0)

    # Newest TRACKED event (across all assets' last activity + this cycle's transfers).
    la_blocks = [int(a["lastActivityBlock"]) for a in assets if a.get("lastActivityBlock")]
    la_times = [int(a["lastActivityAt"]) for a in assets if a.get("lastActivityAt")]
    tx_blocks = [int(t["blockNumber"]) for t in txs if t.get("blockNumber")]
    tx_times = [int(t["blockTimestamp"]) for t in txs if t.get("blockTimestamp")]
    newest_blk = max(la_blocks + tx_blocks, default=None)
    newest_ts = max(la_times + tx_times, default=None)

    # The head was sampled once at cycle START, but the per-token transfer reads run for
    # several seconds after, during which the chain advances — so a just-read event can
    # sit ABOVE that head snapshot (a spurious negative lag). The head is therefore at
    # LEAST where our newest event is; correct it so lag is a true non-negative distance.
    if head is not None and newest_blk is not None:
        head = max(head, newest_blk)

    m: dict = {
        "reportedAt":          now,
        "headBlock":           head,
        "resumeFromBlock":     int(cursor) or None,   # the checkpoint we read above
        "newestTrackedBlock":  newest_blk,
        "newestTrackedAt":     newest_ts,
        "newestTrackedAgeSec": (now - newest_ts) if newest_ts else None,
        "assets":              len(assets),
        "newTransfers":        len(txs),               # emitted THIS cycle (> checkpoint)
    }
    if sec_per_block:
        m["secPerBlockEst"] = round(sec_per_block, 4)
    if head is not None and newest_blk is not None:
        m["lagBlocks"] = head - newest_blk
        if sec_per_block:
            m["lagSecondsEst"] = round((head - newest_blk) * sec_per_block)
    # Coverage of the NEW transfers pulled this cycle (uneven temporal reach lives here).
    if tx_blocks:
        m["cycleOldestBlock"] = min(tx_blocks)
        m["cycleNewestBlock"] = max(tx_blocks)
    if tx_times:
        m["cycleSpanSec"] = max(tx_times) - min(tx_times)

    # Per-asset last-activity age histogram — how stale is each token's newest flow?
    buckets = {"<1h": 0, "1-24h": 0, "1-7d": 0, ">7d": 0, "none": 0}
    for a in assets:
        la = a.get("lastActivityAt")
        if not la:
            buckets["none"] += 1
            continue
        age = now - int(la)
        if age < 3600:
            buckets["<1h"] += 1
        elif age < 86400:
            buckets["1-24h"] += 1
        elif age < 604800:
            buckets["1-7d"] += 1
        else:
            buckets[">7d"] += 1
    m["assetStaleness"] = buckets

    # ── rendered display ────────────────────────────────────────────────────────
    lines = ["[freshness] where the live tail sits in time"]
    if head is not None:
        lag = ""
        if m.get("lagBlocks"):     # non-zero → behind; 0/absent → caught up
            lag = (f"  ·  newest tracked event {m['lagBlocks']:,} blk behind head"
                   + (f" (~{_age_str(m.get('lagSecondsEst'))})" if "lagSecondsEst" in m else ""))
        elif "lagBlocks" in m:
            lag = "  ·  at head (newest tracked event is current)"
        lines.append(f"  head block {head:,}{lag}")
    if newest_blk is not None:
        lines.append(f"  newest tracked event  blk {newest_blk:,}"
                     f"  ({_age_str(m['newestTrackedAgeSec'])} ago)")
    if "cycleOldestBlock" in m:
        lines.append(f"  new this cycle: {len(txs):,} transfer(s) over blk "
                     f"{m['cycleOldestBlock']:,}→{m['cycleNewestBlock']:,} "
                     f"(span {_age_str(m.get('cycleSpanSec'))})")
    else:
        lines.append(f"  new this cycle: 0 transfers past the checkpoint")
    b = buckets
    lines.append(f"  asset flow age  <1h:{b['<1h']}  1-24h:{b['1-24h']}  "
                 f"1-7d:{b['1-7d']}  >7d:{b['>7d']}  none:{b['none']}   ({len(assets)} assets)")
    if sec_per_block:
        lines.append(f"  ~{sec_per_block:.3f} s/block (calibrated from this read)")
    m["display"] = lines
    return m


# ---------------------------------------------------------------------------
# THE SOURCE — read + shape + cursor. Everything else is the harness's job.
# ---------------------------------------------------------------------------
class RobinhoodSource:
    """A live-tail `Source` over Robinhood Chain. Structurally satisfies
    `quickbeam.ingest.scrapers.Source` without importing it (a third-party source
    carries no hard dependency on quickbeam internals)."""

    name = "robinhood"
    # entity_type → volume stem (the volume_<n>_<stem>.json suffix).
    stems = {
        "Asset": "assets", "Transfer": "transfers", "Wallet": "wallets",
        "CorporateAction": "corporateactions", "OracleUpdate": "oracleupdates",
        "LiquidityRebalance": "liquidity", "NewsSentiment": "news",
    }
    # Assets are LIVE snapshots keyed on a stable id (rh:asset:SYM) — latest quote
    # wins, so replace wholesale each cycle. Everything else is a stream of discrete
    # events, ledgered under --accumulate so each commit is a superset.
    snapshot_stems = {"assets"}
    role_map = ROBINHOOD_ROLE_MAP
    presentation = ROBINHOOD_PRESENTATION
    default_volume = 1

    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        """Source-only flags. The shared ones (--output-dir/--volume/--watch/
        --poll-interval/--accumulate/--checkpoint-file + the publish group) are added
        by the harness before this."""
        p.add_argument("--rpc-url", default=None,
                       help=f"Override the Robinhood-Chain JSON-RPC URL (default "
                            f"{ROBINHOOD_RPC_URL}, chain id {ROBINHOOD_CHAIN_ID}).")
        p.add_argument("--blockscout-url", default=None,
                       help=f"Override the Blockscout explorer API (default "
                            f"{ROBINHOOD_BLOCKSCOUT}).")
        p.add_argument("--max-assets", type=int, default=0,
                       help="Cap the number of tokenized stocks read (0 = all).")
        p.add_argument("--with-transfers", action="store_true",
                       help="Also read each token's recent on-chain ERC-20 Transfer "
                            "flow: adds recentVolume/recentTransfers to each Asset and "
                            "emits the largest transfers as linked Transfer nodes (a 2nd "
                            "entity type + edges). One extra Blockscout call per token.")
        p.add_argument("--max-transfers", type=int, default=5,
                       help="How many recent transfers to collect per token "
                            "(--with-transfers). PAGINATED: values above ~50 walk "
                            "Blockscout's transfer pages. Raise this (e.g. 500) to "
                            "capture real flow depth instead of the newest ~50. The robust "
                            "flow metrics (circularity/HHI/interArrivalCV) sharpen as this "
                            "grows — a 5-transfer window can't distinguish wash from organic.")
        p.add_argument("--with-holders", action="store_true",
                       help="Also read each token's holder distribution (one extra bounded "
                            "call per token): adds activeHolders/dustHolderShare/topHolderShare "
                            "so a token sprayed to thousands of dust wallets stops reading as "
                            "'widely held'. Pages only to the ~$1 dust line, so it's cheap.")
        p.add_argument("--block-gt", type=int, default=0,
                       help="Only read transfers with blockNumber greater than this (a "
                            "manual one-shot floor; --checkpoint-file is the persisted "
                            "live-tail equivalent).")
        p.add_argument("--start-block", type=int, default=0,
                       help="Block to begin reading transfer flow from: emit only "
                            "transfers with blockNumber > max(START, checkpoint). Asset "
                            "snapshots are always emitted. A live floor, not a backfill.")

    def read(self, cursor: int, args: argparse.Namespace) -> list[dict]:
        """Read the live chain. The effective transfer floor is max(--block-gt,
        --start-block, checkpoint) so the tail resumes above the last block seen."""
        floor = max(args.block_gt, args.start_block, cursor)
        return read_robinhood_events(
            args.rpc_url, blockscout_url=args.blockscout_url,
            block_gt=floor, max_assets=args.max_assets,
            with_transfers=args.with_transfers, max_transfers=args.max_transfers,
            with_holders=getattr(args, "with_holders", False))

    def build_graph(self, records: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
        return build_graph(records)

    def next_cursor(self, records: list[dict], prev: int) -> int:
        """Advance to the highest transfer block actually read (asset snapshots are
        stamped at chain head, so exclude them or the floor would jump to head and drop
        transfers that land in lower blocks next cycle). Returns `prev` if no flow."""
        tx_blocks = [int(e.get("blockNumber", 0) or 0)
                     for e in records if e.get("type") == "transfer"]
        return max([prev, *tx_blocks]) if tx_blocks else prev

    def freshness_report(self, records: list[dict], cursor: int) -> dict | None:
        """Optional harness hook: a per-cycle freshness/lag summary (see the module-level
        `freshness_report`). The harness prints its `display` lines and persists the rest
        under `<name>Freshness` in the checkpoint file."""
        return freshness_report(records, cursor)


def run() -> None:
    """Console-script / entry-point target: hand the Source to the harness."""
    from quickbeam.ingest.scrapers.harness import run_source
    run_source(RobinhoodSource())


if __name__ == "__main__":
    run()
