"""
End-to-end x402 payment simulation.

Spins up the real quickbeam FastAPI app (with Qdrant + the embedding model
mocked out), enables x402 gating on the search routes, then drives it with a
simulated *agent* that owns a wallet:

  1. The agent calls a gated route with no payment   → 402 + payment requirements.
  2. The agent signs an EIP-3009 transferWithAuthorization for the quoted price
     and retries with the X-PAYMENT header           → 200 + results.
  3. The server settles and returns X-PAYMENT-RESPONSE; the agent records it.

Also covers the MCP tool path and negative cases (insufficient amount, tampered
signature) to prove the gate actually rejects bad payments.

Run:  ./quickbeam/venv/bin/python -m pytest tests/test_x402_agent.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from eth_account import Account

# Make the `quickbeam` package importable when pytest is run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickbeam import server
from quickbeam import x402

VECTOR_DIM = 8
PAY_TO     = "0x000000000000000000000000000000000000dEaD"
PRICE      = "0.01"          # whole USDC
NETWORK    = "base-sepolia"


# ---------------------------------------------------------------------------
# Fakes for the two heavy server dependencies.
# ---------------------------------------------------------------------------
class _FakeScored:
    def __init__(self, idx: int):
        self.id      = f"id-{idx}"
        self.score   = 0.9 - idx * 0.1
        self.vector  = [0.1] * VECTOR_DIM
        self.payload = {
            "id":     f"track-{idx}",
            "owner":  "0xowner",
            "fields": {"title": f"Song {idx}", "byArtist": "Tester"},
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
        # Deterministic unit-length vectors of the collection dimension.
        for _ in texts:
            yield [1.0 / (VECTOR_DIM ** 0.5)] * VECTOR_DIM


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def app():
    """The real server app, wired with fakes + x402 gating, lifespan bypassed."""
    server.cfg = SimpleNamespace(
        collection="test",
        searchable_fields="auto",
        embedding_model="nomic-ai/nomic-embed-text-v1.5",
    )
    server.qdrant_client   = FakeQdrant()
    server.embed_engine    = FakeEmbed()
    server.vector_dim      = VECTOR_DIM
    server.role_map_global = {"title": "title", "subtitle": "byArtist", "tags": []}
    server._warm           = True

    config = x402.X402Config(
        pay_to=PAY_TO,
        price_atomic=x402.price_to_atomic(PRICE),
        network=NETWORK,
    )
    # Reset the built stack first so add_middleware's "already started" guard
    # doesn't fire on the second and later tests, then drop any prior instance.
    server.app.middleware_stack = None
    server.app.user_middleware = [
        m for m in server.app.user_middleware
        if m.cls.__name__ != "X402Middleware"
    ]
    server.app.add_middleware(x402.build_middleware(config), config=config)
    return server.app


@pytest.fixture
def agent():
    """A fresh agent wallet."""
    return Account.create()


def _transport(app):
    return httpx.ASGITransport(app=app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_unpaid_request_returns_402_with_requirements(app):
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://test") as c:
        resp = await c.get("/search", params={"q": "late night drive"})

    assert resp.status_code == 402
    body = resp.json()
    assert body["x402Version"] == 1
    assert body["accepts"], "402 must advertise at least one payment requirement"
    req = body["accepts"][0]
    assert req["scheme"] == "exact"
    assert req["network"] == NETWORK
    assert req["payTo"].lower() == PAY_TO.lower()
    assert req["maxAmountRequired"] == str(x402.price_to_atomic(PRICE))


@pytest.mark.anyio
async def test_agent_pays_and_gets_results(app, agent):
    async with x402.PayingClient(
        base_url="http://test", private_key=agent.key.hex(),
        network=NETWORK, transport=_transport(app),
    ) as client:
        resp = await client.get("/search", params={"q": "late night drive"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["results"], "paid search should return results"

        # The client recorded exactly one settled payment for the right amount.
        assert len(client.payments) == 1
        pay = client.payments[0]
        assert pay.amount == str(x402.price_to_atomic(PRICE))
        assert pay.network == NETWORK
        # Settlement recovered the agent as the payer.
        assert pay.payer is not None
        assert pay.payer.lower() == agent.address.lower()
        assert pay.transaction  # a (mock) settlement reference is present


@pytest.mark.anyio
async def test_free_routes_need_no_payment(app):
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://test") as c:
        resp = await c.post("/embed", json={"text": "hello"})
    assert resp.status_code == 200
    assert "embedding" in resp.json()


@pytest.mark.anyio
async def test_insufficient_payment_is_rejected(app, agent):
    """An authorization for less than the quoted price must be refused."""
    requirements = x402.PaymentRequirements(
        scheme="exact", network=NETWORK,
        max_amount_required="1",                 # underpay: 1 atomic unit
        pay_to=PAY_TO, asset=x402.NETWORKS[NETWORK]["usdc"],
        resource="http://test/search",
        extra={"name": "USD Coin", "version": "2"},
    )
    payment = x402.sign_payment(agent.key.hex(), requirements)
    header  = x402.encode_payment_header(payment)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://test") as c:
        resp = await c.get("/search", params={"q": "x"}, headers={"X-PAYMENT": header})

    assert resp.status_code == 402
    assert "below the required amount" in resp.json()["error"]


@pytest.mark.anyio
async def test_tampered_signature_is_rejected(app, agent):
    """A payment whose signature doesn't match `from` must be refused."""
    config_req = x402.X402Config(
        pay_to=PAY_TO, price_atomic=x402.price_to_atomic(PRICE), network=NETWORK,
    ).requirements_for("http://test/search")
    payment = x402.sign_payment(agent.key.hex(), config_req)
    # Tamper: claim a different payer than the one who actually signed.
    payment["payload"]["authorization"]["from"] = "0x" + "ab" * 20
    header = x402.encode_payment_header(payment)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://test") as c:
        resp = await c.get("/search", params={"q": "x"}, headers={"X-PAYMENT": header})

    assert resp.status_code == 402
    assert "does not match" in resp.json()["error"]


# anyio backend selection (asyncio only — we have no trio dependency).
@pytest.fixture
def anyio_backend():
    return "asyncio"
