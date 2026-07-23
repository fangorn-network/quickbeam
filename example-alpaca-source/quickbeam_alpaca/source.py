"""
An Alpaca Market-Data `Source` for the quickbeam ingestion harness.

This is an EXAMPLE of the pluggable-source pattern (a sibling of
example-robinhood-source): quickbeam core ships no concrete sources, so a data
source lives in its own package and registers a `quickbeam.sources` entry point
(see ../pyproject.toml). Installing this package makes `quickbeam data alpaca`
work with the full harness loop — CLI, staged-volume emission, checkpointing, the
`--watch` daemon, and `--publish` to fangorn — with zero changes to quickbeam.

WHAT A SOURCE SUPPLIES
----------------------
Only read + shape + cursor; everything generic is the harness's job:

  read(cursor, args)      → raw event dicts from the upstream (Alpaca REST)
  build_graph(records)    → ({entityType: [{name, fields}]}, [edge]) — PURE, testable
  next_cursor(records, p) → the checkpoint to persist (max crawl day, YYYYMMDD int)

A CRAWL IS A (DAY, SYMBOL SET)
------------------------------
The upstream is Alpaca's Market Data v2 REST API (free IEX feed by default). One
crawl covers a well-defined trading day for a well-defined symbol universe. The
default universe is the most-actives screener (the liquid names worth embedding);
the default day is the latest available session. Re-crawling the same day is a
no-op upsert (Assets are snapshots keyed on symbol, latest bar wins); tomorrow's
crawl advances the cursor. Pass `--symbols AAPL,MSFT,...` to pin the universe or
`--day 2026-07-16` to pin the day.

Two entity types:
  Asset     — one daily-bar snapshot per symbol (OHLCV, change%, vwap). Snapshot.
  NewsItem  — recent Alpaca news headlines, linked Asset --hasNews--> NewsItem.
              Discrete events (ledgered under --accumulate), and the richest
              semantic signal to embed (real prose vs. OHLCV numbers).

Env `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` (or --api-key/--api-secret) authenticate.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

ALPACA_DATA_URL = "https://data.alpaca.markets"
# Trading API base for /v2/assets. Paper and live are SEPARATE key namespaces — a key
# authenticates on exactly one, and free/new accounts get paper keys — so default to
# paper (the assets list is identical on both). Override with APCA_API_BASE_URL or
# --trading-url if you hold live keys.
ALPACA_TRADING_URL = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

# ---------------------------------------------------------------------------
# ROLE MAP + PRESENTATION — the field roles the harness `--dry-run` preview uses
# to show what the embed loop would index. The high-value semantic signal is the
# verbalized `text` blurb; price/change are structured measures for hybrid
# filtering, not embedded prose.
# ---------------------------------------------------------------------------
ALPACA_ROLE_MAP: dict = {
    "title":    "symbol",
    "subtitle": "name",
    "tags":     ["exchange", "signal"],
    "text":     ["text"],
}

ALPACA_PRESENTATION: dict = {
    "accent": "#ffd500",  # Alpaca yellow
    "icons": {
        "Asset":    "trending_up",
        "NewsItem": "newspaper",
    },
}


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


def _fmt_usd(x):
    x = _num(x)
    if x is None:
        return None
    if x == 0:
        return "$0"
    if abs(x) < 0.01:
        return "<$0.01"
    return f"${x:,.0f}" if abs(x) >= 1000 else f"${x:,.2f}"


def _fmt_vol(v):
    """Share volume with a K/M/B suffix, or None."""
    v = _num(v)
    if v is None:
        return None
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:,.1f}{unit}"
    return f"{v:,.0f}"


def verbalize(ev: dict) -> str:
    """Human-readable blurb for an event — this is what gets embedded. Deterministic
    (no wall-clock in the text) so a re-shaped event embeds identically."""
    t = ev.get("type")
    sym = ev.get("symbol", "?")
    name = ev.get("name") or sym
    if t == "asset":
        exch = ev.get("exchange") or "US equity"
        parts = [f"{name} ({sym}) is a {exch} stock"]
        close = _num(ev.get("close"))
        if close is not None:
            parts.append(f", closed ${close:,.2f} on {ev.get('day', '?')}")
        chg = _pct(ev.get("close"), ev.get("prevClose"))
        if chg is not None:
            parts.append(f" ({chg:+.2f}% vs prior close)")
        vol = _fmt_vol(ev.get("volume"))
        if vol:
            parts.append(f" on {vol} shares")
        parts.append(".")
        lo, hi = _num(ev.get("low")), _num(ev.get("high"))
        if lo is not None and hi is not None:
            parts.append(f" Day range ${lo:,.2f}–${hi:,.2f}.")
        vwap = _num(ev.get("vwap"))
        if vwap is not None:
            parts.append(f" VWAP ${vwap:,.2f}.")
        return "".join(parts)
    if t == "news":
        headline = ev.get("headline", "")
        summary = ev.get("summary", "")
        src = ev.get("source", "wire")
        when = ev.get("day", "")
        return (f"News on {name} ({sym}) [{src}{', ' + when if when else ''}]: "
                f"{headline}. {summary}").strip()
    return f"{name} ({sym}) event."


def _signal(ev: dict) -> str:
    """A coarse, filterable taste facet for an Asset (drives hybrid filters + tags)."""
    if ev.get("type") != "asset":
        return "news"
    chg = _pct(ev.get("close"), ev.get("prevClose"))
    if chg is None:
        return "flat"
    if chg >= 3:
        return "strong_up"
    if chg > 0:
        return "up"
    if chg <= -3:
        return "strong_down"
    if chg < 0:
        return "down"
    return "flat"


def node_id(ev: dict) -> str:
    """Stable node id. Asset snapshots collapse to one id per SYMBOL (a live quote that
    OVERWRITES); news items get a unique id so each headline is its own record."""
    sym = ev.get("symbol", "?")
    if ev.get("type") == "asset":
        return f"ap:asset:{sym}"
    h = hashlib.sha256((ev.get("id") or ev.get("headline", "")).encode()).hexdigest()[:16]
    return f"ap:news:{sym}:{h}"


def shape_fields(ev: dict) -> dict:
    """One raw event → the `fields` dict for its node (text blurb + structured measures)."""
    t = ev.get("type")
    base = {
        "entityType": "Asset" if t == "asset" else "NewsItem",
        "symbol": ev.get("symbol"),
        "name": ev.get("name") or ev.get("symbol"),
        "signal": _signal(ev),
        "text": verbalize(ev),
    }
    if t == "asset":
        base.update({
            "exchange": ev.get("exchange"),
            "day": ev.get("day"),
            "open": _num(ev.get("open")), "high": _num(ev.get("high")),
            "low": _num(ev.get("low")), "close": _num(ev.get("close")),
            "prevClose": _num(ev.get("prevClose")),
            "volume": _num(ev.get("volume")), "vwap": _num(ev.get("vwap")),
            "changePct": _pct(ev.get("close"), ev.get("prevClose")),
        })
    else:
        base.update({
            "source": ev.get("source"), "day": ev.get("day"),
            "headline": ev.get("headline"), "url": ev.get("url"),
        })
    return {k: v for k, v in base.items() if v is not None}


def build_graph(events: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Return ({entityType: [{"name", "fields"}]}, [edge...]). PURE — no I/O, so a unit
    test hand-builds events, calls it, and asserts on the nodes/edges. Asset snapshots
    dedup per symbol (latest wins); news items dedup by their unique id. Each news item
    links back to its Asset via `hasNews` (a synthetic Asset is made if the symbol had
    no bar this crawl, so the edge always has a valid source)."""
    assets: dict[str, dict] = {}
    news: dict[str, dict] = {}
    edges: list[dict] = []

    def _ensure_asset(sym: str) -> str:
        aid = f"ap:asset:{sym}"
        if aid not in assets:
            assets[aid] = {"name": aid, "fields": {
                "entityType": "Asset", "symbol": sym, "name": sym,
                "signal": "flat", "text": f"{sym} — US equity.",
            }}
        return aid

    for ev in events:
        t = ev.get("type")
        sym = ev.get("symbol")
        if not sym:
            continue
        nid = node_id(ev)
        if t == "asset":
            assets[nid] = {"name": nid, "fields": shape_fields(ev)}
        elif t == "news":
            news[nid] = {"name": nid, "fields": shape_fields(ev)}
            aid = _ensure_asset(sym)
            edges.append({"rel": "hasNews", "from": aid, "to": nid,
                          "fromType": "Asset", "toType": "NewsItem"})

    nodes: dict[str, list[dict]] = {}
    if assets:
        nodes["Asset"] = list(assets.values())
    if news:
        nodes["NewsItem"] = list(news.values())
    return nodes, edges


# ---------------------------------------------------------------------------
# LIVE READ — all network IO lives here. IPv4-pinned urlopen with a bounded retry
# on transient upstream failures (mirrors the robinhood source's proven helper).
# ---------------------------------------------------------------------------
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, *args, **kwargs):
    return _orig_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)


def _http_json(url: str, headers: dict, *, timeout: float = 20.0,
               retries: int = 2, backoff: float = 1.5):
    """urlopen → parsed JSON, IPv4 pinned, per-attempt timeout, short retry on 5xx /
    connection errors. 4xx and the final attempt raise; the caller decides fatal vs skip."""
    req = urllib.request.Request(url, headers=headers)
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
    raise last  # unreachable; satisfies type-checkers


def _auth_headers(api_key: str | None, api_secret: str | None) -> dict:
    key = api_key or os.environ.get("APCA_API_KEY_ID")
    secret = api_secret or os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Alpaca credentials missing: set APCA_API_KEY_ID / "
                         "APCA_API_SECRET_KEY (or pass --api-key / --api-secret).")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json"}


def _most_actives(headers: dict, top: int) -> list[str]:
    """The default universe: Alpaca's most-actives screener. One call, always-liquid
    names, nothing to maintain."""
    url = f"{ALPACA_DATA_URL}/v1beta1/screener/stocks/most-actives?top={top}"
    data = _http_json(url, headers)
    return [r["symbol"] for r in data.get("most_actives", []) if r.get("symbol")]


def _all_assets(headers: dict, trading_url: str = ALPACA_TRADING_URL) -> dict[str, dict]:
    """The FULL universe: every active, tradable US equity from the Trading API's
    /v2/assets (thousands of symbols). Returns {symbol: {name, exchange}} so Asset
    nodes carry real company names — the bars feed only has a one-letter exchange code
    and no name. One un-paginated call; Alpaca returns the whole list at once."""
    url = f"{trading_url}/v2/assets?status=active&asset_class=us_equity"
    out: dict[str, dict] = {}
    for a in _http_json(url, headers, timeout=60.0):
        sym = a.get("symbol")
        # `tradable` weeds out delisted/halted shells that have no recent bars anyway.
        if sym and a.get("tradable"):
            out[sym] = {"name": a.get("name") or sym, "exchange": a.get("exchange")}
    return out


def _session_day(day: str | None) -> str:
    """The trading day to crawl (YYYY-MM-DD). Default = today; the bars call itself
    returns the latest available session's bar when today has none yet (weekend/pre-open),
    so we don't need a market calendar for the prototype."""
    return day or dt.date.today().isoformat()


def read_alpaca_events(*, api_key: str | None = None, api_secret: str | None = None,
                       symbols: list[str] | None = None, top: int = 100,
                       all_assets: bool = False, trading_url: str = ALPACA_TRADING_URL,
                       day: str | None = None, feed: str = "iex",
                       with_news: bool = True, news_limit: int = 10) -> list[dict]:
    """Read one crawl: daily-bar Asset snapshots for the universe, plus recent news.

    Universe precedence: explicit `symbols` > `all_assets` (every active US equity) >
    the most-actives screener (top N). The bars call requests a short window ending on
    `day` and keeps the last two bars per symbol, so the day's OHLCV AND the prior close
    come from one call (change% is then local)."""
    headers = _auth_headers(api_key, api_secret)
    day = _session_day(day)
    meta: dict[str, dict] = {}                 # symbol -> {name, exchange}
    if symbols:
        syms = symbols
    elif all_assets:
        meta = _all_assets(headers, trading_url)
        syms = list(meta)
    else:
        syms = _most_actives(headers, top)
    if not syms:
        return []

    events: list[dict] = []
    # Window: 7 calendar days back through `day` — enough to guarantee a prior trading
    # bar even across a long weekend. We keep only the last two bars per symbol.
    start = (dt.date.fromisoformat(day) - dt.timedelta(days=7)).isoformat()
    bars = _fetch_bars(headers, syms, start, day, feed)
    for sym, sym_bars in bars.items():
        if not sym_bars:
            continue
        last = sym_bars[-1]
        prev = sym_bars[-2] if len(sym_bars) >= 2 else {}
        m = meta.get(sym, {})
        events.append({
            "type": "asset", "symbol": sym, "name": m.get("name") or sym,
            "exchange": m.get("exchange") or last.get("x"),
            "day": (last.get("t") or day)[:10],
            "open": last.get("o"), "high": last.get("h"), "low": last.get("l"),
            "close": last.get("c"), "prevClose": prev.get("c"),
            "volume": last.get("v"), "vwap": last.get("vw"),
        })

    if with_news:
        events.extend(_fetch_news(headers, syms, day, news_limit))
    return events


def _fetch_bars(headers: dict, symbols: list[str], start: str, end: str,
                feed: str) -> dict[str, list[dict]]:
    """Multi-symbol daily bars, following `next_page_token`. Alpaca caps ~10k symbols
    per query-string; the most-actives universe (≤100) fits one URL. For big universes
    we chunk at 200 symbols to stay well under the URL limit."""
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), 200):
        chunk = ",".join(symbols[i:i + 200])
        token = None
        while True:
            params = {"symbols": chunk, "timeframe": "1Day", "start": start,
                      "end": end, "feed": feed, "limit": 1000, "adjustment": "raw"}
            if token:
                params["page_token"] = token
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            data = _http_json(f"{ALPACA_DATA_URL}/v2/stocks/bars?{qs}", headers)
            for sym, sym_bars in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(sym_bars)
            token = data.get("next_page_token")
            if not token:
                break
    return out


def _fetch_news(headers: dict, symbols: list[str], day: str, limit: int) -> list[dict]:
    """Recent news for the universe. One call (Alpaca's /news is multi-symbol); each
    article carries its own symbol list, so we fan it out to one NewsItem per (symbol,
    article) that intersects our universe."""
    sym_set = set(symbols)
    # A universe of thousands would blow the URL length, so above ~200 symbols we ask
    # for market-wide latest news and filter to our universe locally.
    sym_q = "" if len(symbols) > 200 else f"symbols={','.join(symbols)}&"
    url = (f"{ALPACA_DATA_URL}/v1beta1/news?{sym_q}limit={limit}"
           f"&sort=desc&include_content=false")
    try:
        data = _http_json(url, headers)
    except urllib.error.HTTPError:
        return []  # news is optional signal; never let it fail a crawl
    events: list[dict] = []
    for art in data.get("news", []):
        created = (art.get("created_at") or "")[:10]
        for sym in art.get("symbols", []):
            if sym not in sym_set:
                continue
            events.append({
                "type": "news", "symbol": sym, "name": sym,
                "id": f"{art.get('id')}:{sym}",
                "headline": art.get("headline", ""),
                "summary": (art.get("summary") or "")[:500],
                "source": art.get("source", "wire"),
                "url": art.get("url"), "day": created or day,
            })
    return events


# ---------------------------------------------------------------------------
# THE SOURCE — structurally satisfies quickbeam.ingest.scrapers.Source.
# ---------------------------------------------------------------------------
class AlpacaSource:
    """A daily-crawl `Source` over Alpaca Market Data. Structurally satisfies
    `quickbeam.ingest.scrapers.Source` without importing it."""

    name = "alpaca"
    stems = {"Asset": "assets", "NewsItem": "news"}
    # Assets are the latest daily bar per symbol → snapshot (rewritten each crawl).
    # News is a stream of discrete items → ledgered under --accumulate.
    snapshot_stems = {"assets"}
    role_map = ALPACA_ROLE_MAP
    presentation = ALPACA_PRESENTATION
    default_volume = 1

    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--api-key", default=None,
                       help="Alpaca API key id (default env APCA_API_KEY_ID).")
        p.add_argument("--api-secret", default=None,
                       help="Alpaca API secret (default env APCA_API_SECRET_KEY).")
        p.add_argument("--symbols", default=None,
                       help="Comma-separated universe to pin (e.g. AAPL,MSFT,NVDA). "
                            "Default: the most-actives screener (--top).")
        p.add_argument("--top", type=int, default=100,
                       help="Most-actives universe size when --symbols is unset (default 100).")
        p.add_argument("--all-assets", action="store_true",
                       help="Crawl EVERY active, tradable US equity (thousands of symbols) "
                            "from /v2/assets, not just the most-actives screener. Symbols "
                            "with no bar for the day are silently skipped.")
        p.add_argument("--trading-url", default=ALPACA_TRADING_URL,
                       help=f"Trading API base for --all-assets (default {ALPACA_TRADING_URL}). "
                            f"Paper and live keys are separate; use the base matching your key.")
        p.add_argument("--day", default=None,
                       help="Trading day to crawl, YYYY-MM-DD (default: latest available).")
        p.add_argument("--feed", default="iex", choices=["iex", "sip", "delayed_sip"],
                       help="Market-data feed (default iex, the free tier).")
        p.add_argument("--no-news", action="store_true",
                       help="Skip the news feed (Assets only).")
        p.add_argument("--news-limit", type=int, default=10,
                       help="Max news articles to pull per crawl (default 10).")

    def read(self, cursor: int, args: argparse.Namespace) -> list[dict]:
        syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                if args.symbols else None)
        return read_alpaca_events(
            api_key=args.api_key, api_secret=args.api_secret,
            symbols=syms, top=args.top, all_assets=args.all_assets,
            trading_url=args.trading_url, day=args.day, feed=args.feed,
            with_news=not args.no_news, news_limit=args.news_limit)

    def build_graph(self, records: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
        return build_graph(records)

    def next_cursor(self, records: list[dict], prev: int) -> int:
        """The crawl day as a YYYYMMDD int. Re-crawling the same day keeps `prev`
        (upsert no-op); a later day advances it."""
        days = [int(e["day"].replace("-", "")) for e in records
                if e.get("type") == "asset" and e.get("day")]
        return max([prev, *days]) if days else prev


def run() -> None:
    """Console-script / entry-point target: hand the Source to the harness."""
    from quickbeam.ingest.scrapers.harness import run_source
    run_source(AlpacaSource())


if __name__ == "__main__":
    run()
