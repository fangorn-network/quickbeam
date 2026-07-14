"""Unit tests for the Robinhood `Source` — the PURE shaper (`build_graph`) and the
cursor logic. No network, no DB: hand-build events, call the pure functions, assert.
This is exactly the payoff of the harness split — the source's whole testable core is
`build_graph`/`next_cursor`, and the harness's staging/checkpoint/publish is tested
once in quickbeam, not per source."""
import time

from quickbeam_robinhood.source import (RobinhoodSource, build_graph,
                                        freshness_report, node_id, shape_fields,
                                        verbalize)


def _asset(sym, **kw):
    return {"type": "asset", "symbol": sym, "name": kw.pop("name", sym),
            "sector": kw.pop("sector", "equity"), **kw}


def _transfer(sym, txHash, **kw):
    return {"type": "transfer", "symbol": sym, "txHash": txHash,
            "logIndex": kw.pop("logIndex", 0), "value": kw.pop("value", 1.0), **kw}


def test_asset_node_id_is_stable_per_symbol():
    # An asset is a live quote: the same symbol collapses to one id (upsert), so two
    # snapshots of NVDA overwrite rather than duplicate.
    assert node_id(_asset("NVDA")) == "rh:asset:NVDA"
    assert node_id(_asset("NVDA", price=200)) == node_id(_asset("NVDA", price=999))


def test_transfer_node_id_is_unique_per_event():
    a = _transfer("TSLA", "0xaaa", logIndex=1)
    b = _transfer("TSLA", "0xbbb", logIndex=2)
    assert node_id(a) != node_id(b)
    assert node_id(a) == "rh:xfer:0xaaa:1"


def test_asset_blurb_leads_with_business_profile():
    # The curated profile must PREFIX the stat-line so the embedding is dominated by
    # what the company does (this is what makes semantic search discriminate).
    text = verbalize(_asset("NVDA", price=194.42, marketCap=800717, holders=97))
    assert text.startswith("NVIDIA designs")
    assert "$194.42" in text and "800,717" in text and "97 on-chain holders" in text


def test_asset_without_profile_falls_through_to_statline():
    text = verbalize(_asset("ZZZZ", name="Zeta", price=1.0))
    assert text.startswith("Zeta (ZZZZ) is a tokenized equity stock")


def test_build_graph_dedups_assets_keeps_latest_and_links_transfers():
    events = [
        _asset("TSLA", name="Tesla", sector="automotive", price=100, blockNumber=10),
        _asset("TSLA", name="Tesla", sector="automotive", price=110, blockNumber=20),
        _transfer("TSLA", "0xdead", logIndex=0, value=5.0,
                  fromAddr="0xAbc", toAddr="0xDef", blockNumber=21),
    ]
    nodes, edges = build_graph(events)

    # One Asset for TSLA, latest snapshot (price 110) wins.
    assets = nodes["Asset"]
    assert len(assets) == 1
    assert assets[0]["fields"]["price"] == 110.0

    # The transfer is its own node, linked from the Asset by hasTransfer.
    assert len(nodes["Transfer"]) == 1
    rels = {(e["rel"], e["from"], e["to"]) for e in edges}
    assert ("hasTransfer", "rh:asset:TSLA", "rh:xfer:0xdead:0") in rels

    # Wallet endpoints are promoted to first-class nodes (lowercased ids) and linked.
    wallet_ids = {w["name"] for w in nodes["Wallet"]}
    assert "rh:wallet:0xabc" in wallet_ids and "rh:wallet:0xdef" in wallet_ids
    assert ("sentBy", "rh:xfer:0xdead:0", "rh:wallet:0xabc") in rels
    assert ("receivedBy", "rh:xfer:0xdead:0", "rh:wallet:0xdef") in rels


def test_build_graph_synthesizes_asset_for_event_only_symbol():
    # A transfer for a symbol never seen as an asset still gets a minimal Asset node
    # so the hasTransfer edge has a valid source.
    nodes, edges = build_graph([_transfer("GHOST", "0x1", blockNumber=5)])
    assert [a["name"] for a in nodes["Asset"]] == ["rh:asset:GHOST"]


def test_asset_fields_omit_read_time_block_but_transfers_keep_it():
    # Asset snapshots are live quotes stamped at head — don't index their block/ts
    # (they'd read as "everything happened now"). Transfers carry real event time.
    af = shape_fields(_asset("AAPL", price=1, blockNumber=999, blockTimestamp=123))
    assert "blockNumber" not in af and "timestamp" not in af
    tf = shape_fields(_transfer("AAPL", "0x9", blockNumber=999, blockTimestamp=123))
    assert tf["blockNumber"] == 999 and tf["timestamp"] == 123


def test_asset_carries_observed_and_activity_time_not_as_event_time():
    # The honest time model: an Asset gets `observedAt` (read-time, as staleness
    # metadata) and — with flow — `lastActivityAt`/`lastActivityBlock` (REAL chain
    # time of its latest transfer). Neither poses as the event `timestamp`, and none
    # leak into the embedded blurb (which stays semantic, not read-time noise).
    af = shape_fields(_asset("NVDA", observedAt=1_700_000_000, lastActivityAt=1_699_000_000,
                             lastActivityBlock=555, recentTransfers=3))
    assert af["observedAt"] == 1_700_000_000 and af["lastActivityAt"] == 1_699_000_000
    assert af["lastActivityBlock"] == 555
    assert "timestamp" not in af and "blockNumber" not in af  # not event time
    assert "1700000000" not in af["text"] and "1699000000" not in af["text"]


def test_asset_blurb_describes_trust_not_raw_counts():
    # The embedded activity text must describe the ROBUST character of the flow, never the
    # gameable raw count/volume — those are the cheapest thing for a wash ring to inflate,
    # and baking them into the vector lets washed tokens retrieve on "actively traded".
    organic = verbalize(_asset("NVDA", dataQuality="organic", sampleSize=400,
                               recentTransfers=400, recentVolumeUsd=250000))
    assert "organically traded" in organic and "broadly distributed" in organic
    assert "400 recent" not in organic and "$250,000" not in organic   # no raw leak
    suspect = verbalize(_asset("TSLA", dataQuality="suspect", sampleSize=500))
    assert "manipulated" in suspect and "circular" in suspect
    quiet = verbalize(_asset("ZZZ", name="Zeta", sampleSize=0, recentTransfers=0))
    assert "Quiet: no recent on-chain flow" in quiet


def test_asset_signal_prefers_robust_trust_facet():
    # `active` used to be just recentTransfers > 0 — a cron satisfies that forever. The
    # signal now prefers the robustness-derived dataQuality facet.
    from quickbeam_robinhood.source import _signal
    assert _signal(_asset("NVDA", dataQuality="organic")) == "active-organic"
    assert _signal(_asset("NVDA", dataQuality="suspect")) == "wash-suspect"
    assert _signal(_asset("NVDA", dataQuality="sparse")) == "thin"
    # Backward-compat: a flow read without metrics still degrades to active/quiet/listed.
    assert _signal(_asset("NVDA", recentTransfers=4)) == "active"
    assert _signal(_asset("NVDA", recentTransfers=0)) == "quiet"
    assert _signal(_asset("NVDA")) == "listed"          # no flow read at all


def test_flow_metrics_flags_circular_wash():
    # A↔B round-trip of one fixed parcel on a metronome: gross volume is high but net
    # displacement collapses, one sender pair, identical amounts, constant gaps — every
    # robust signal fires, so the composite reads "suspect".
    from quickbeam_robinhood.source import _flow_metrics
    a, b = "0xA", "0xB"
    xf = [{"value": 100.0, "from": (a if i % 2 == 0 else b),
           "to": (b if i % 2 == 0 else a), "ts": 1000 + i * 3600} for i in range(6)]
    m = _flow_metrics(xf)
    assert m["circularityRatio"] == 1.0      # net displacement is zero
    assert m["amountQuantization"] == 1.0    # one fixed parcel size
    assert m["interArrivalCV"] == 0.0        # perfect cron heartbeat
    assert m["senderHHI"] == 0.5             # two equal senders
    assert m["manipulationScore"] >= 0.66 and m["dataQuality"] == "suspect"


def test_flow_metrics_reads_organic_flow_as_broad():
    # Many distinct senders, one-way flow to distinct receivers, varied sizes, bursty
    # gaps → low circularity/HHI, high arrival variance → organic.
    from quickbeam_robinhood.source import _flow_metrics
    xf = [
        {"value": 12.0, "from": "0x1", "to": "0x9", "ts": 1000},
        {"value": 3.5,  "from": "0x2", "to": "0x8", "ts": 1005},
        {"value": 47.0, "from": "0x3", "to": "0x7", "ts": 1200},
        {"value": 8.1,  "from": "0x4", "to": "0x6", "ts": 5000},
        {"value": 21.3, "from": "0x5", "to": "0xA", "ts": 5002},
    ]
    m = _flow_metrics(xf)
    assert m["circularityRatio"] == 0.0      # pure one-way displacement
    assert m["uniqueSenders"] == 5 and m["distinctCounterparties"] == 10
    assert m["senderHHI"] < 0.4
    assert m["dataQuality"] == "organic"


def test_shape_fields_indexes_flow_metrics_but_keeps_them_out_of_text():
    f = shape_fields(_asset("NVDA", price=100, dataQuality="suspect",
                            circularityRatio=0.95, senderHHI=0.8, manipulationScore=0.88,
                            uniqueSenders=2, sampleSize=50, recentTransfers=50))
    # Indexed as filterable measures, with counts as ints...
    assert f["circularityRatio"] == 0.95 and f["manipulationScore"] == 0.88
    assert f["dataQuality"] == "suspect"
    assert f["uniqueSenders"] == 2 and isinstance(f["uniqueSenders"], int)
    assert isinstance(f["sampleSize"], int)
    # ...but the raw metric numbers never leak into the embedded text.
    for leak in ("0.95", "0.88", "0.8"):
        assert leak not in f["text"]


def test_holder_metrics_flags_dust_dominated_token():
    # A token with 4,203 "holders" but only a handful holding real balance: dustHolderShare
    # → ~1.0, and the whale grip shows in topHolderShare.
    from quickbeam_robinhood.source import _holder_metrics
    m = _holder_metrics([500.0, 300.0, 150.0, 50.0], holders_count=4203, hit_threshold_wall=True)
    assert m["activeHolders"] == 4
    assert m["dustHolderShare"] > 0.99
    assert m["topHolderShare"] == 0.5            # 500 / 1000
    assert "activeHoldersIsLowerBound" not in m  # we hit the dust wall, count is exact


def test_transfer_blurb_states_usd_and_real_block_time():
    # A transfer must be self-describing for retrieval: USD size (find whales) + the
    # REAL block date (ground "recent"), both from the event so the text is deterministic.
    text = verbalize(_transfer("TSLA", "0x1", value=1000.0, usdValue=425000,
                               blockTimestamp=1_700_000_000, fromAddr="0xAbc", toAddr="0xDef"))
    assert "~$425,000" in text
    assert "2023-11-14" in text                          # 1_700_000_000 → UTC date
    # A transfer with no block time still verbalizes (time simply omitted, not faked).
    assert "on " not in verbalize(_transfer("TSLA", "0x2", value=1.0))


def test_next_cursor_advances_only_on_transfer_blocks():
    src = RobinhoodSource()
    # Asset-only read: cursor must not move (snapshots are stamped at head).
    assert src.next_cursor([_asset("NVDA", blockNumber=9_999)], prev=100) == 100
    # A transfer moves the floor to its block.
    recs = [_asset("NVDA", blockNumber=9_999), _transfer("NVDA", "0x1", blockNumber=250)]
    assert src.next_cursor(recs, prev=100) == 250
    # Never regresses below prev.
    assert src.next_cursor([_transfer("NVDA", "0x1", blockNumber=50)], prev=100) == 100


def test_freshness_report_measures_lag_and_staleness():
    now = int(time.time())
    recs = [
        # newest tracked event at blk 995 / 5m ago; head sampled at 1000 → 5 blk behind
        _asset("NVDA", observedHead=1000, observedAt=now,
               lastActivityBlock=995, lastActivityAt=now - 300),
        _asset("AAPL", observedHead=1000, observedAt=now,       # ~25h stale → 1-7d bucket
               lastActivityBlock=900, lastActivityAt=now - 90_000),
        _asset("GME", observedHead=1000, observedAt=now),        # no flow
        _transfer("NVDA", "0x1", blockNumber=995, blockTimestamp=now - 300),
        _transfer("NVDA", "0x2", blockNumber=990, blockTimestamp=now - 600),
    ]
    rep = freshness_report(recs, cursor=980)
    assert rep["lagBlocks"] == 5                       # 1000 - 995
    assert rep["newestTrackedBlock"] == 995
    assert rep["newTransfers"] == 2                    # transfers emitted this cycle
    assert rep["resumeFromBlock"] == 980               # the checkpoint we read from
    assert rep["assetStaleness"] == {"<1h": 1, "1-24h": 0, "1-7d": 1, ">7d": 0, "none": 1}
    assert rep["display"]                              # renders lines to print


def test_freshness_report_never_reports_negative_lag():
    # The head is sampled at cycle start; a transfer read a few seconds later can land
    # ABOVE it. The head is corrected to at least the newest event → lag floors at 0.
    now = int(time.time())
    recs = [_asset("NVDA", observedHead=1000, observedAt=now,
                   lastActivityBlock=1005, lastActivityAt=now - 10)]
    rep = freshness_report(recs, cursor=0)
    assert rep["lagBlocks"] == 0
    assert rep["headBlock"] == 1005                    # head raised to the newest event


def test_freshness_report_empty_is_none():
    assert freshness_report([], 0) is None
