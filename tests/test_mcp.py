"""
MCP layer tests.

Phase 1 — the MCP tools are a thin client of an *ungated* localhost API:
  • semantic_search returns clean, schema-generic results (title/subtitle/tags/
    score), carries on-chain provenance on every hit, and drops the embedding.
  • corpus_info describes the corpus.

Phase 2 — per-tool x402 payment (isolated in mcp_payments):
  • an unpaid call returns the x402 requirements as the tool result.
  • the agent signs the quoted requirement and retries with `payment=<b64>`;
    the call then returns results plus a settlement receipt.

The MCP client is pointed at the in-process FastAPI app via httpx ASGITransport,
so no network or Qdrant is involved.

Run:  ./quickbeam/venv/bin/python -m pytest tests/test_mcp.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickbeam import server
from quickbeam import mcp_server
from quickbeam import mcp_payments
from quickbeam import x402

VECTOR_DIM = 8
TS         = 1_700_000_000           # fixed publish timestamp for provenance
PAY_TO     = "0x000000000000000000000000000000000000dEaD"
PRICE      = "0.005"
NETWORK    = "base-sepolia"


# ---------------------------------------------------------------------------
# Fakes (payload now includes `meta` so provenance flows through _hit_from_point)
# ---------------------------------------------------------------------------
class _FakeScored:
    def __init__(self, idx: int):
        self.id      = f"id-{idx}"
        self.score   = 0.9 - idx * 0.1
        self.vector  = [0.1] * VECTOR_DIM
        self.payload = {
            "id":     f"track-{idx}",
            "owner":  "0xowner",
            "fields": {"title": f"Song {idx}", "byArtist": "Tester",
                       "genres": ["ambient", "downtempo"]},
            "meta":   {"manifestCid": f"cid-{idx}", "blockTimestamp": TS,
                       "version": 1, "owner": "0xpublisher"},
        }


class _FakeQueryResp:
    def __init__(self, n: int):
        self.points = [_FakeScored(i) for i in range(n)]


class FakeQdrant:
    def query_points(self, collection_name, query, limit, **kwargs):
        return _FakeQueryResp(min(limit, 3))

    def scroll(self, collection_name, limit, offset=0, **kwargs):
        return ([_FakeScored(i) for i in range(min(limit, 3))], None)

    def get_collection(self, name):
        return SimpleNamespace(points_count=3)


class FakeEmbed:
    def embed(self, texts, batch_size=64):
        for _ in texts:
            yield [1.0 / (VECTOR_DIM ** 0.5)] * VECTOR_DIM


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def app():
    """The real server app with fakes, NO x402 middleware (MCP calls localhost
    ungated in Phase 1)."""
    server.cfg = SimpleNamespace(
        collection="test", searchable_fields="auto",
        embedding_model="nomic-ai/nomic-embed-text-v1.5",
    )
    server.qdrant_client   = FakeQdrant()
    server.embed_engine    = FakeEmbed()
    server.vector_dim      = VECTOR_DIM
    server.role_map_global = {"title": "title", "subtitle": "byArtist", "tags": ["genres"]}
    server._warm           = True
    # Strip any x402 middleware a sibling test may have installed on the shared app.
    server.app.middleware_stack = None
    server.app.user_middleware = [
        m for m in server.app.user_middleware if m.cls.__name__ != "X402Middleware"
    ]
    return server.app


@pytest.fixture(autouse=True)
def reset_mcp(app, monkeypatch):
    """Point the MCP client at the in-process app and reset its module state."""
    monkeypatch.setattr(mcp_server, "_client",
                        lambda: httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=app),
                            base_url="http://test"))
    monkeypatch.setattr(mcp_server, "_roles_cache", None)
    monkeypatch.setattr(mcp_server, "_GATE", None)
    monkeypatch.setattr(mcp_server, "CORPUS", "fangorn-music")


@pytest.fixture
def agent():
    return Account.create()


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_semantic_search_shapes_results_with_provenance(app):
    out = await mcp_server.semantic_search(query="late night ambient drive", limit=5)

    assert out["corpus"] == "fangorn-music"
    assert out["results"], "should return shaped results"

    hit = out["results"][0]
    # Schema-generic shape from the role map.
    assert hit["title"] == "Song 0"
    assert hit["subtitle"] == "Tester"
    assert hit["tags"] == ["ambient", "downtempo"]
    assert isinstance(hit["score"], float)
    # Embedding is dropped; raw fields are not leaked.
    assert "embedding" not in hit
    assert "fields" not in hit
    # Provenance is first-class on every result.
    prov = hit["provenance"]
    assert prov["source_cid"] == "cid-0"
    assert prov["publisher"] == "0xpublisher"
    assert prov["version"] == 1
    assert prov["published"].startswith("20")  # ISO8601 from blockTimestamp


@pytest.mark.anyio
async def test_corpus_info(app):
    info = await mcp_server.corpus_info()
    assert info["corpus"] == "fangorn-music"
    assert info["record_count"] == 3
    assert info["roles"]["title"] == "title"
    assert info["roles"]["tags"] == ["genres"]
    assert info["paid"] is False


@pytest.mark.anyio
async def test_phase1_takes_no_payment_argument_path(app):
    """With no gate, passing a payment is simply ignored — tools stay free."""
    out = await mcp_server.semantic_search(query="x", limit=1, payment="ignored")
    assert out["results"]
    assert "payment" not in out


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------
@pytest.fixture
def gated(monkeypatch):
    gate = mcp_payments.build_gate(pay_to=PAY_TO, price=PRICE, network=NETWORK)
    monkeypatch.setattr(mcp_server, "_GATE", gate)
    return gate


@pytest.mark.anyio
async def test_unpaid_call_returns_x402_challenge(app, gated):
    out = await mcp_server.semantic_search(query="ambient drive", limit=5)
    assert out.get("payment_required") is True
    assert out["accepts"], "challenge must carry payment requirements"
    req = out["accepts"][0]
    assert req["payTo"].lower() == PAY_TO.lower()
    assert req["maxAmountRequired"] == str(x402.price_to_atomic(PRICE))
    assert "results" not in out


@pytest.mark.anyio
async def test_paid_call_returns_results_and_receipt(app, gated, agent):
    # 1) Probe to obtain the requirements.
    challenge = await mcp_server.semantic_search(query="ambient drive", limit=5)
    requirements = x402.PaymentRequirements.from_dict(challenge["accepts"][0])

    # 2) Agent signs and retries with the payment proof.
    payment = x402.sign_payment(agent.key.hex(), requirements)
    header  = x402.encode_payment_header(payment)
    out     = await mcp_server.semantic_search(query="ambient drive", limit=5, payment=header)

    assert "results" in out and out["results"]
    receipt = out["payment"]
    assert receipt["payer"].lower() == agent.address.lower()
    assert receipt["amount"] == str(x402.price_to_atomic(PRICE))
    assert receipt["transaction"]


@pytest.mark.anyio
async def test_underpayment_is_rejected(app, gated, agent):
    bad_req = x402.PaymentRequirements(
        scheme="exact", network=NETWORK, max_amount_required="1",
        pay_to=PAY_TO, asset=x402.NETWORKS[NETWORK]["usdc"],
        resource="mcp://semantic_search",
        extra={"name": "USD Coin", "version": "2"},
    )
    payment = x402.sign_payment(agent.key.hex(), bad_req)
    header  = x402.encode_payment_header(payment)
    out     = await mcp_server.semantic_search(query="x", limit=1, payment=header)

    assert out.get("payment_required") is True
    assert "below the required amount" in out["error"]
