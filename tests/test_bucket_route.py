"""Guards for GET /bucket/{id} — the private-retrieval route.

Every call passes owner/limit explicitly: FastAPI resolves `Query(...)` defaults
through dependency injection, so invoking the coroutine directly would hand the
function the Query objects themselves.

server.py had no test coverage at all, so this also establishes the pattern: its
`cfg` and `qdrant_client` are plain module globals, so monkeypatching them and
awaiting the route function directly avoids both a live Qdrant and the lifespan
(which would try to build a real client).

The properties worth pinning are privacy properties, and they fail silently:
a route that quietly accepted a cell list, or that returned a partial page, would
still look like it worked.
"""
import argparse
import asyncio

import pytest

from quickbeam import server
from quickbeam.index import cells_in_bucket


class FakeRecord:
    def __init__(self, pid, cell, owner="0xowner"):
        self.id = pid
        self.payload = {"id": f"track:{pid}", "fields": {"title": f"t{pid}", "cell": cell},
                        "owner": owner, "meta": {"sourceCid": "cid"}}
        self.vector = [0.1, 0.2, 0.3]


class FakeQdrant:
    """Pages 2 records at a time so the cursor loop is actually exercised."""

    def __init__(self, records):
        self.records = records
        self.calls = []

    def scroll(self, collection_name, scroll_filter, limit, offset,
               with_payload, with_vectors):
        self.calls.append({"filter": scroll_filter, "offset": offset,
                           "with_vectors": with_vectors})
        start = offset or 0
        page = self.records[start:start + 2]
        nxt = start + 2 if start + 2 < len(self.records) else None
        return page, nxt


@pytest.fixture
def wired(monkeypatch):
    layout = {"k": 6, "nbuckets": 3, "buckets": [0, 1, 2, 0, 1, 2]}
    recs = [FakeRecord(i, cell=0 if i % 2 else 3) for i in range(5)]
    fake = FakeQdrant(recs)
    monkeypatch.setattr(server, "_layout", layout)
    monkeypatch.setattr(server, "qdrant_client", fake)
    monkeypatch.setattr(server, "cfg", argparse.Namespace(collection="test"))
    return fake, layout


def test_bucket_expands_server_side_to_all_its_cells(wired):
    """The server does bucket -> cells, so a caller cannot request ONE cell and
    opt out of its own anonymity set. If this inverts, the anonymity set is 1."""
    fake, layout = wired
    out = asyncio.run(server.bucket(0, owner=None, limit=0))
    assert out["cells"] == [0, 3] == cells_in_bucket(layout, 0)
    match = fake.calls[0]["filter"].must[0].match
    assert sorted(match.any) == [0, 3], "must filter on the whole bucket, not one cell"


def test_bucket_scroll_pages_to_completion(wired):
    """next_page_offset is a cursor, not a skip count. Stopping at the first page
    would return a third of the bucket and still look like a success."""
    fake, _ = wired
    out = asyncio.run(server.bucket(0, owner=None, limit=0))
    assert out["count"] == 5
    assert len(fake.calls) == 3          # 2 + 2 + 1


def test_bucket_returns_vectors_for_client_side_reranking(wired):
    """The client re-ranks against its true query, which is impossible without
    document vectors. with_vectors=False would silently degrade every result."""
    fake, _ = wired
    out = asyncio.run(server.bucket(0, owner=None, limit=0))
    assert all(c["with_vectors"] for c in fake.calls)
    assert all("embedding" in r for r in out["results"])


def test_bucket_limit_truncates(wired):
    out = asyncio.run(server.bucket(0, owner=None, limit=3))
    assert out["count"] == 3


def test_unknown_bucket_404s(wired):
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.bucket(99, owner=None, limit=0))
    assert e.value.status_code == 404


def test_route_is_501_without_a_layout(monkeypatch):
    """No layout means no server-side bucket expansion, and serving anything then
    would mean trusting a client-supplied cell list."""
    monkeypatch.setattr(server, "_layout", None)
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.bucket(0, owner=None, limit=0))
    assert e.value.status_code == 501


def test_owner_scope_is_additive_not_replacing(wired):
    """Scoping must AND with the cell filter. Replacing it would serve the whole
    collection for that owner — every cell, i.e. no privacy at all."""
    fake, _ = wired
    asyncio.run(server.bucket(0, owner="0xowner", limit=0))
    must = fake.calls[0]["filter"].must
    assert len(must) == 2
    assert must[0].key == "cell" and must[1].key == "owner"
