"""
MCP layer tests — the SELF-CONTAINED, local pull-client (mcp_server.py).

The MCP pulls a dataset's shards from the Semantic CDN into an in-memory index and
searches them LOCALLY. These tests fake the CDN (catalog / manifest / shard bytes)
and the query embedder, so no network, no Qdrant, and no model download is
involved — the same isolation the module gives an agent at runtime.

Coverage
  • list_datasets  — the catalog, hiding empty datasets.
  • describe       — entity types, raw field vocab, relation types, embed contract.
  • search         — semantic ranking over RAW records + provenance; entityType /
                     owner pre-filters.
  • get            — one record by id (== its graph node endpoint).
  • neighbors      — typed linkset traversal (out/in/rel filter; in- and
                     out-of-corpus endpoints).
  • embed transform— the query goes through the SAME matryoshka transform as docs.
  • Phase 2 x402   — an unpaid `search` returns the challenge; a signed retry
                     returns results + a receipt; underpayment is rejected.

Run:  ./venv/bin/python -m pytest tests/test_mcp.py -v
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickbeam import mcp_server

# The x402 payment path needs eth_account (signing). It's optional here: when
# absent, only the Phase-2 tests skip; the core pull-client tests still run.
try:
    from eth_account import Account
    from quickbeam import mcp_payments
    from quickbeam import x402
    _HAS_X402 = True
except ModuleNotFoundError:
    _HAS_X402 = False

requires_x402 = pytest.mark.skipif(not _HAS_X402, reason="eth_account not installed")

TS      = 1_700_000_000                 # fixed publish timestamp for provenance
PAY_TO  = "0x000000000000000000000000000000000000dEaD"
PRICE   = "0.005"
NETWORK = "base-sepolia"


# ---------------------------------------------------------------------------
# Fake Semantic CDN — a robinhood-shaped dataset of tokenized assets.
# ---------------------------------------------------------------------------
DIM = 4

# Each row's id is its graph node endpoint; edges reference these same ids.
_ROWS = [
    {"track_id": "rh:asset:NVDA", "owner": "0xA",
     "fields": {"entityType": "Asset", "symbol": "NVDA", "name": "NVIDIA",
                "sector": "Semiconductors"},
     "embedding": [1.0, 0.0, 0.0, 0.0],
     "meta": {"manifestCid": "cidNVDA", "blockTimestamp": TS, "version": 1,
              "owner": "0xpublisher"}},
    {"track_id": "rh:asset:GME", "owner": "0xA",
     "fields": {"entityType": "Asset", "symbol": "GME", "name": "GameStop",
                "sector": "Retail"},
     "embedding": [0.0, 1.0, 0.0, 0.0],
     "meta": {"manifestCid": "cidGME", "blockTimestamp": TS, "version": 1,
              "owner": "0xpublisher"}},
    {"track_id": "rh:asset:COIN", "owner": "0xB",
     "fields": {"entityType": "Business", "symbol": "COIN", "name": "Coinbase"},
     "embedding": [0.0, 0.0, 1.0, 0.0],
     "meta": {"manifestCid": "cidCOIN", "blockTimestamp": TS, "version": 1,
              "owner": "0xpublisher"}},
]

_MANIFEST = {
    "name": "robinhood", "description": "Tokenized-stock universe",
    "model": "nomic-ai/nomic-embed-text-v1.5", "dim": DIM, "distance": "Cosine",
    "entity_types": [{"type": "Asset", "count": 2}, {"type": "Business", "count": 1}],
    "shards": [{"file": "shard-0000-abc.ndjson.gz", "count": len(_ROWS)}],
}

_CATALOG = {
    "collection": "robinhood",
    "domains": [
        {"name": "robinhood", "description": "Tokenized-stock universe", "count": 3,
         "dim": DIM, "entity_types": [{"type": "Asset", "count": 2}]},
        # An empty (unbaked) domain — list_datasets must hide it.
        {"name": "music", "description": "MusicBrainz", "count": 0, "dim": DIM,
         "entity_types": []},
    ],
}

_EDGES = [
    {"rel": "hasTransfer", "from": "rh:asset:NVDA", "to": "rh:xfer:0xabc:1",
     "fromType": "Asset", "toType": "Transfer"},
    {"rel": "hasAction", "from": "rh:asset:NVDA", "to": "rh:ca:NVDA:2026:split",
     "fromType": "Asset", "toType": "CorporateAction"},
    {"rel": "hasTransfer", "from": "rh:asset:GME", "to": "rh:xfer:0xdef:2",
     "fromType": "Asset", "toType": "Transfer"},
]


def _shard_bytes(rows) -> bytes:
    return gzip.compress("\n".join(json.dumps(r) for r in rows).encode("utf-8"))


async def _fake_get_json(path: str) -> dict:
    if path == "/catalog":
        return _CATALOG
    if path == "/domains/robinhood/manifest":
        return _MANIFEST
    raise KeyError(path)          # e.g. /domains/robinhood/edges → local fallback


async def _fake_get_bytes(path: str) -> bytes:
    if path.startswith("/domains/robinhood/shards/"):
        return _shard_bytes(_ROWS)
    raise KeyError(path)


async def _fake_embed_query(model: str, dim: int, text: str) -> np.ndarray:
    """Deterministic query vector so ranking is predictable: any query mentioning
    'gpu'/'chip' points at NVDA; otherwise at GME."""
    v = [1.0, 0.0, 0.0, 0.0] if any(t in text.lower() for t in ("gpu", "chip")) \
        else [0.0, 1.0, 0.0, 0.0]
    return np.asarray(v[:dim], dtype=np.float32)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_mcp(monkeypatch, tmp_path):
    """Reset module state and point the CDN client + embedder at the fakes."""
    monkeypatch.setattr(mcp_server, "_get_json", _fake_get_json)
    monkeypatch.setattr(mcp_server, "_get_bytes", _fake_get_bytes)
    monkeypatch.setattr(mcp_server, "_embed_query", _fake_embed_query)
    monkeypatch.setattr(mcp_server, "_REGISTRY", {})
    monkeypatch.setattr(mcp_server, "_GATE", None)
    # Relational axis: a local linkset file (the shape linkgen/robinhood stage).
    edges_file = tmp_path / "robinhood.json"
    edges_file.write_text(json.dumps(_EDGES))
    monkeypatch.setattr(mcp_server, "EDGES_PATH", str(tmp_path))


@pytest.fixture
def agent():
    return Account.create()  # only used by @requires_x402 tests


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Discovery / description
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_list_datasets_hides_empty():
    out = await mcp_server.list_datasets()
    names = [d["name"] for d in out["datasets"]]
    assert names == ["robinhood"]          # empty "music" domain is hidden
    assert out["collection"] == "robinhood"


@pytest.mark.anyio
async def test_describe_exposes_raw_fields_and_relations():
    out = await mcp_server.describe("robinhood")
    assert out["count"] == 3
    assert out["embed"] == {"model": _MANIFEST["model"], "dim": DIM, "distance": "Cosine"}
    # Raw field vocabulary (no role_map projection).
    assert {"symbol", "name", "sector", "entityType"} <= set(out["fields"])
    # Relation types from the linkset drive `neighbors`.
    assert set(out["relations"]) == {"hasTransfer", "hasAction"}
    assert out["relational_axis"] == "available"


# ---------------------------------------------------------------------------
# Semantic axis
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_search_returns_raw_records_and_provenance():
    out = await mcp_server.search(dataset="robinhood", query="AI gpu chipmaker", limit=5)
    assert out["dataset"] == "robinhood"
    hit = out["results"][0]
    assert hit["id"] == "rh:asset:NVDA"          # query steered to NVDA
    assert hit["entityType"] == "Asset"
    # RAW fields, not a title/subtitle/tags projection.
    assert hit["fields"]["symbol"] == "NVDA"
    assert hit["fields"]["sector"] == "Semiconductors"
    assert isinstance(hit["score"], float)
    prov = hit["provenance"]
    assert prov["source_cid"] == "cidNVDA"
    assert prov["publisher"] == "0xpublisher"
    assert prov["published"].startswith("20")     # ISO8601 from blockTimestamp


@pytest.mark.anyio
async def test_search_ranks_by_similarity():
    out = await mcp_server.search(dataset="robinhood", query="meme stock", limit=5)
    assert out["results"][0]["id"] == "rh:asset:GME"


@pytest.mark.anyio
async def test_search_entity_type_filter():
    out = await mcp_server.search(dataset="robinhood", query="anything",
                                  entity_type="Business", limit=5)
    ids = [h["id"] for h in out["results"]]
    assert ids == ["rh:asset:COIN"]               # only the Business survives


@pytest.mark.anyio
async def test_search_owner_filter():
    out = await mcp_server.search(dataset="robinhood", query="meme stock",
                                  owner="0xB", limit=5)
    assert [h["id"] for h in out["results"]] == ["rh:asset:COIN"]


@pytest.mark.anyio
async def test_get_by_id():
    out = await mcp_server.get(dataset="robinhood", id="rh:asset:GME")
    assert out["record"]["fields"]["name"] == "GameStop"
    missing = await mcp_server.get(dataset="robinhood", id="rh:asset:NOPE")
    assert missing["error"] == "not_found"


# ---------------------------------------------------------------------------
# Relational axis
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_neighbors_walks_out_edges():
    out = await mcp_server.neighbors(dataset="robinhood", id="rh:asset:NVDA")
    rels = sorted(n["rel"] for n in out["neighbors"])
    assert rels == ["hasAction", "hasTransfer"]   # only NVDA's edges, not GME's
    for n in out["neighbors"]:
        assert n["direction"] == "out"
        # Neighbors are Transfer/CorporateAction nodes outside this Asset corpus,
        # so they come back as endpoints (id + type) with no fields.
        assert "fields" not in n


@pytest.mark.anyio
async def test_neighbors_rel_filter():
    out = await mcp_server.neighbors(dataset="robinhood", id="rh:asset:NVDA",
                                     rel="hasTransfer")
    assert [n["id"] for n in out["neighbors"]] == ["rh:xfer:0xabc:1"]


@pytest.mark.anyio
async def test_neighbors_in_direction_resolves_in_corpus_record():
    out = await mcp_server.neighbors(dataset="robinhood", id="rh:asset:NVDA",
                                     direction="in")
    assert out["neighbors"] == []                 # nothing points *at* NVDA
    back = await mcp_server.neighbors(dataset="robinhood", id="rh:xfer:0xabc:1",
                                      direction="in")
    (edge,) = back["neighbors"]
    assert edge["direction"] == "in" and edge["id"] == "rh:asset:NVDA"
    assert edge["fields"]["symbol"] == "NVDA"      # neighbor is in-corpus → fields


@pytest.mark.anyio
async def test_neighbors_without_linkset(monkeypatch):
    monkeypatch.setattr(mcp_server, "EDGES_PATH", None)
    out = await mcp_server.neighbors(dataset="robinhood", id="rh:asset:NVDA")
    assert out["neighbors"] == []
    assert "not delivered" in out["note"]


@pytest.mark.anyio
async def test_neighbors_prefers_cdn_edges(monkeypatch):
    """When the CDN delivers a linkset (/domains/{name}/edges), it is used and the
    local QUICKBEAM_EDGES fallback is not needed."""
    async def cdn_with_edges(path):
        if path == "/domains/robinhood/edges":
            return {"count": 1, "edges": [
                {"rel": "hasNews", "from": "rh:asset:NVDA", "to": "rh:news:NVDA:x",
                 "fromType": "Asset", "toType": "NewsSentiment"}]}
        return await _fake_get_json(path)

    monkeypatch.setattr(mcp_server, "_get_json", cdn_with_edges)
    monkeypatch.setattr(mcp_server, "EDGES_PATH", None)   # no local fallback
    out = await mcp_server.neighbors(dataset="robinhood", id="rh:asset:NVDA")
    assert [n["rel"] for n in out["neighbors"]] == ["hasNews"]


# ---------------------------------------------------------------------------
# Query-embedding transform parity (the correctness-critical seam)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_embed_query_uses_matryoshka(monkeypatch):
    """The query must go through the same transform as documents: prefixed,
    LayerNorm'd, sliced to dim, and L2-normalized. Exercises the REAL
    _embed_query (reset_mcp stubs it, so restore it here) with a fake raw engine."""
    monkeypatch.undo()
    captured = {}

    def fake_raw(model, texts):
        captured["texts"] = texts
        return [[0.2, 0.9, -0.3, 0.5, 0.1, 0.0, 0.7, -0.4]]   # full-width

    monkeypatch.setattr(mcp_server, "_embed_raw", fake_raw)
    q = await mcp_server._embed_query("m", DIM, "late night gpu drive")
    assert captured["texts"] == ["search_query: late night gpu drive"]
    assert q.shape == (DIM,)
    assert np.isclose(np.linalg.norm(q), 1.0, atol=1e-5)      # L2-normalized


# ---------------------------------------------------------------------------
# Phase 2 — per-tool x402 payment (gate now sits on `search`)
# ---------------------------------------------------------------------------
@pytest.fixture
def gated(monkeypatch):
    gate = mcp_payments.build_gate(pay_to=PAY_TO, price=PRICE, network=NETWORK)
    monkeypatch.setattr(mcp_server, "_GATE", gate)
    return gate


@requires_x402
@pytest.mark.anyio
async def test_unpaid_search_returns_x402_challenge(gated):
    out = await mcp_server.search(dataset="robinhood", query="gpu", limit=5)
    assert out.get("payment_required") is True
    assert out["accepts"], "challenge must carry payment requirements"
    assert out["accepts"][0]["payTo"].lower() == PAY_TO.lower()
    assert "results" not in out


@requires_x402
@pytest.mark.anyio
async def test_paid_search_returns_results_and_receipt(gated, agent):
    challenge = await mcp_server.search(dataset="robinhood", query="gpu", limit=5)
    requirements = x402.PaymentRequirements.from_dict(challenge["accepts"][0])
    payment = x402.sign_payment(agent.key.hex(), requirements)
    header = x402.encode_payment_header(payment)

    out = await mcp_server.search(dataset="robinhood", query="gpu chip", limit=5,
                                  payment=header)
    assert out["results"][0]["id"] == "rh:asset:NVDA"
    receipt = out["payment"]
    assert receipt["payer"].lower() == agent.address.lower()
    assert receipt["amount"] == str(x402.price_to_atomic(PRICE))


@requires_x402
@pytest.mark.anyio
async def test_underpayment_is_rejected(gated, agent):
    bad_req = x402.PaymentRequirements(
        scheme="exact", network=NETWORK, max_amount_required="1",
        pay_to=PAY_TO, asset=x402.NETWORKS[NETWORK]["usdc"],
        resource="mcp://search",
        extra={"name": "USD Coin", "version": "2"},
    )
    payment = x402.sign_payment(agent.key.hex(), bad_req)
    header = x402.encode_payment_header(payment)
    out = await mcp_server.search(dataset="robinhood", query="gpu", limit=5,
                                  payment=header)
    assert out.get("payment_required") is True
    assert "below the required amount" in out["error"]
