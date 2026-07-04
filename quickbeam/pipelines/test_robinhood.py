"""
test_robinhood.py — the pure shaper + graph builder. No Qdrant, no fastembed, no
live chain: just the event-dict → node/record contract everything downstream
depends on. The event dicts below are hand-built unit-test inputs — they exercise
the shaper directly and stand in for what `_read_robinhood_chain` yields live.
"""
import json

from quickbeam.pipelines.robinhood import (
    ROBINHOOD_ROLE_MAP,
    build_graph,
    compose_searchable_text,
    emit_volumes,
    node_id,
    shape_event,
    verbalize,
)

# One representative event of each of the five types — enough to exercise every
# branch of the shaper without generating a synthetic feed.
_SAMPLE_EVENTS = [
    {"type": "asset", "symbol": "AAPL", "name": "Apple", "sector": "technology",
     "price": 232.5, "dayChangePct": 1.4, "marketCap": 3_500_000_000,
     "holders": 41, "blockNumber": 10, "blockTimestamp": 1_700_000_010},
    {"type": "oracle_update", "symbol": "TSLA", "name": "Tesla",
     "oldPrice": 100.0, "newPrice": 108.0, "oracle": "chainlink",
     "blockNumber": 11, "blockTimestamp": 1_700_000_011},
    {"type": "news_sentiment", "symbol": "NVDA", "name": "NVIDIA",
     "headline": "beats quarterly earnings", "source": "Reuters", "sentiment": 0.5,
     "summary": "Strong data-center demand.", "blockNumber": 12,
     "blockTimestamp": 1_700_000_012},
    {"type": "corporate_action", "symbol": "MSFT", "name": "Microsoft",
     "actionType": "dividend", "detail": "Quarterly dividend of $0.75/share.",
     "exDate": "2026-08-14", "blockNumber": 13, "blockTimestamp": 1_700_000_013},
    {"type": "liquidity_rebalance", "symbol": "COIN", "name": "Coinbase",
     "pool": "COIN-USDC", "oldDepth": 1_000_000, "newDepth": 1_500_000,
     "blockNumber": 14, "blockTimestamp": 1_700_000_014},
]


def _required_record_shape(r: dict) -> None:
    # The exact keys embeddings._embed_and_upload and cdn._shard_row read.
    assert isinstance(r["track_id"], str) and r["track_id"]
    assert "entityType" in r["fields"]
    for k in ("owner", "manifestCid", "blockNumber", "blockTimestamp"):
        assert k in r["meta"], f"missing meta.{k}"
    assert isinstance(r["meta"]["blockNumber"], int)


def test_shapes_all_event_types():
    seen = set()
    for e in _SAMPLE_EVENTS:
        r = shape_event(e)
        _required_record_shape(r)
        assert r["fields"]["text"], "text (the embeddable blurb) must be non-empty"
        seen.add(r["entity_type"])
    assert seen == {"Asset", "CorporateAction", "OracleUpdate",
                    "LiquidityRebalance", "NewsSentiment"}, seen


def test_asset_id_idempotent_but_events_unique():
    a1 = shape_event({"type": "asset", "symbol": "AAPL", "price": 100, "blockNumber": 1})
    a2 = shape_event({"type": "asset", "symbol": "AAPL", "price": 111, "blockNumber": 9})
    assert a1["track_id"] == a2["track_id"]
    o1 = node_id({"type": "oracle_update", "symbol": "AAPL", "blockNumber": 1})
    o2 = node_id({"type": "oracle_update", "symbol": "AAPL", "blockNumber": 2})
    assert o1 != o2


def test_oracle_deviation_and_signal():
    r = shape_event({"type": "oracle_update", "symbol": "TSLA", "name": "Tesla",
                     "oldPrice": 100, "newPrice": 108, "blockNumber": 5})
    assert r["fields"]["deviationPct"] == 8.0
    assert r["fields"]["signal"] == "oracle-spike"   # |dev| >= 5
    assert "8.00%" in verbalize({"type": "oracle_update", "symbol": "TSLA",
                                 "oldPrice": 100, "newPrice": 108})


def test_news_sentiment_tone():
    r = shape_event({"type": "news_sentiment", "symbol": "NVDA", "name": "Nvidia",
                     "headline": "beats", "sentiment": 0.5, "blockNumber": 3})
    assert r["fields"]["signal"] == "bullish"
    assert "Bullish" in r["fields"]["text"]


def test_asset_text_leads_with_business_profile():
    # Curated _PROFILES description must lead the embedded blurb so the vector is
    # dominated by WHAT the company does, not the shared stat-line boilerplate.
    r = shape_event({"type": "asset", "symbol": "NVDA", "name": "NVIDIA",
                     "sector": "semiconductors", "price": 194.5, "blockNumber": 8})
    assert r["fields"]["text"].startswith("NVIDIA designs")
    assert "GPUs" in r["fields"]["text"]


def test_compose_text_uses_role_map():
    r = shape_event({"type": "asset", "symbol": "MSFT", "name": "Microsoft",
                     "sector": "technology", "price": 400, "dayChangePct": 1.2,
                     "blockNumber": 7})
    text = compose_searchable_text(r["fields"], ROBINHOOD_ROLE_MAP)
    assert text.startswith("search_document: ")
    assert "MSFT" in text                          # title role (symbol)
    assert "technology" in text                    # a tag role
    assert "tokenized technology stock" in text    # text role (the blurb)


# ── The graph / volume emitter ────────────────────────────────────────────────
def test_build_graph_links_events_to_one_asset():
    events = [
        {"type": "asset", "symbol": "AAPL", "name": "Apple", "sector": "tech",
         "price": 100, "blockNumber": 1},
        {"type": "asset", "symbol": "AAPL", "name": "Apple", "sector": "tech",
         "price": 130, "blockNumber": 5},                       # newer snapshot wins
        {"type": "oracle_update", "symbol": "AAPL", "oldPrice": 100,
         "newPrice": 110, "blockNumber": 6},
        {"type": "news_sentiment", "symbol": "TSLA", "name": "Tesla",
         "headline": "x", "sentiment": 0.3, "blockNumber": 7},  # TSLA seen only via news
    ]
    nodes, edges = build_graph(events)
    # one Asset per symbol; AAPL keeps the latest ($130) snapshot.
    assets = {n["name"]: n for n in nodes["Asset"]}
    assert set(assets) == {"rh:asset:AAPL", "rh:asset:TSLA"}
    assert assets["rh:asset:AAPL"]["fields"]["price"] == 130
    # TSLA had no snapshot → a minimal Asset node was synthesized.
    assert assets["rh:asset:TSLA"]["fields"]["entityType"] == "Asset"
    # every non-asset event is linked from its Asset by a typed edge.
    rels = {(e["from"], e["rel"], e["toType"]) for e in edges}
    assert ("rh:asset:AAPL", "hasOracleUpdate", "OracleUpdate") in rels
    assert ("rh:asset:TSLA", "hasNews", "NewsSentiment") in rels


def test_transfer_shapes_and_links_to_asset():
    ev = {"type": "transfer", "symbol": "NVDA", "name": "NVIDIA",
          "sector": "semiconductors", "value": 4.62, "fromAddr": "0xAAAA1111",
          "toAddr": "0xBBBB2222", "txHash": "0xdead", "logIndex": 3,
          "blockNumber": 100, "blockTimestamp": 1_700_000_100}
    r = shape_event(ev)
    assert r["entity_type"] == "Transfer"
    assert r["track_id"] == "rh:xfer:0xdead:3"       # stable on tx + log index
    assert r["fields"]["value"] == 4.62
    assert r["fields"]["signal"] == "notable-transfer"
    assert "4.62 NVDA" in r["fields"]["text"]
    # Time-ordering: discrete events carry a real, indexed block time + height so
    # downstream can sequence flow (holding periods, before/after splits).
    assert r["fields"]["timestamp"] == 1_700_000_100
    assert r["fields"]["blockNumber"] == 100
    # A transfer links from its Asset (which is synthesized if unseen).
    _nodes, edges = build_graph([ev])
    rels = {(e["from"], e["rel"], e["toType"]) for e in edges}
    assert ("rh:asset:NVDA", "hasTransfer", "Transfer") in rels


def test_asset_snapshot_carries_no_event_timestamp():
    # Asset snapshots are live quotes stamped at chain head — indexing their read-time
    # block/ts would make everything look like it "happened now", so they're excluded.
    r = shape_event({"type": "asset", "symbol": "AAPL", "price": 100,
                     "blockNumber": 10, "blockTimestamp": 1_700_000_010})
    assert "timestamp" not in r["fields"]
    assert "blockNumber" not in r["fields"]


def test_iso_to_epoch_parses_blockscout_timestamps():
    from quickbeam.pipelines.robinhood import _iso_to_epoch
    assert _iso_to_epoch("1970-01-01T00:00:00.000000Z") == 0
    assert _iso_to_epoch("2024-01-15T12:34:56Z") == 1_705_322_096
    assert _iso_to_epoch(None) is None
    assert _iso_to_epoch("not-a-date") is None


def test_emit_volumes_writes_expected_files(tmp_path):
    counts = emit_volumes(_SAMPLE_EVENTS, str(tmp_path), volume=3)
    # Asset + edges files always present.
    assert (tmp_path / "volume_3_assets.json").exists()
    assert (tmp_path / "volume_3_edges.json").exists()
    # Node files are valid JSON arrays of {name, fields{entityType}}.
    assets = json.loads((tmp_path / "volume_3_assets.json").read_text())
    assert assets and all(n["fields"]["entityType"] == "Asset" for n in assets)
    assert counts["Asset"] == len(assets)
