"""Unit tests for the Alpaca `Source` — the PURE shaper (`build_graph`) and cursor
logic. No network: hand-build events, call the pure functions, assert. Everything
generic (staging/checkpoint/publish) is tested once in quickbeam, not here."""
from quickbeam_alpaca.source import (AlpacaSource, build_graph, node_id,
                                     shape_fields, verbalize)


def _asset(sym, close, prev=None, **kw):
    return {"type": "asset", "symbol": sym, "name": kw.pop("name", sym),
            "day": kw.pop("day", "2026-07-16"), "close": close, "prevClose": prev,
            **kw}


def _news(sym, headline, **kw):
    return {"type": "news", "symbol": sym, "headline": headline,
            "id": kw.pop("id", headline), "summary": kw.pop("summary", ""), **kw}


def test_asset_id_is_stable_per_symbol():
    # An asset is a daily snapshot: the same symbol collapses to one id (upsert), so two
    # crawls of it overwrite rather than duplicate.
    assert node_id(_asset("AAPL", 213.4)) == "ap:asset:AAPL"
    nodes, _ = build_graph([_asset("AAPL", 210.0), _asset("AAPL", 213.4)])
    assert len(nodes["Asset"]) == 1
    assert nodes["Asset"][0]["fields"]["close"] == 213.4  # latest wins


def test_change_pct_from_prev_close():
    f = shape_fields(_asset("MSFT", 110.0, prev=100.0))
    assert f["changePct"] == 10.0
    assert f["signal"] == "strong_up"
    # No prior close → no change%, no crash.
    assert "changePct" not in shape_fields(_asset("MSFT", 110.0))


def test_verbalize_is_deterministic_and_carries_signal():
    txt = verbalize(_asset("NVDA", 180.0, prev=175.0, high=182.0, low=176.0,
                           volume=41_200_000, exchange="NASDAQ"))
    assert "NVDA" in txt and "+2.86%" in txt and "41.2M shares" in txt
    assert verbalize(_asset("NVDA", 180.0, prev=175.0)) == \
        verbalize(_asset("NVDA", 180.0, prev=175.0))  # no wall-clock


def test_news_links_to_its_asset():
    nodes, edges = build_graph([_asset("AAPL", 213.4), _news("AAPL", "Apple beats")])
    assert len(nodes["NewsItem"]) == 1
    rels = {(e["rel"], e["from"], e["to"]) for e in edges}
    nid = nodes["NewsItem"][0]["name"]
    assert ("hasNews", "ap:asset:AAPL", nid) in rels


def test_news_for_unseen_symbol_gets_a_synthetic_asset():
    # A headline about a symbol with no bar this crawl still links to a valid Asset.
    nodes, edges = build_graph([_news("TSLA", "Tesla recall")])
    assert len(nodes["Asset"]) == 1                # synthesized
    assert edges[0]["from"] == "ap:asset:TSLA"


def test_next_cursor_advances_to_latest_crawl_day():
    src = AlpacaSource()
    recs = [_asset("AAPL", 1.0, day="2026-07-16"), _news("AAPL", "x")]
    assert src.next_cursor(recs, 0) == 20260716
    # Re-crawling the same/earlier day never rewinds.
    assert src.next_cursor([_asset("AAPL", 1.0, day="2026-07-15")], 20260716) == 20260716
    # News-only read (no asset day) keeps the cursor.
    assert src.next_cursor([_news("AAPL", "x")], 20260716) == 20260716
