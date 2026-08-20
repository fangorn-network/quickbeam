"""Guards for GET /adjacency — the route that makes a non-resident record clickable.

Without it, a search hit fetched from a bucket renders "Not in this snapshot"
(Entity.tsx:77) the moment you click it. These pin the properties that would fail
quietly: a wrong direction returns the wrong half of the graph, a lost edge order
scrambles rails, and a `crosses` flag that never fires silently removes the
cross-publisher badge the whole demo is about.
"""
import argparse
import asyncio
import sqlite3

import pytest

from quickbeam import server


@pytest.fixture
def wired(monkeypatch):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript("""
        CREATE TABLE edge(rel TEXT,src TEXT,dst TEXT,stype TEXT,dtype TEXT);
        INSERT INTO edge VALUES
          ('created','audius:user:A','audius:track:1','Artist','Track'),
          ('created','audius:user:A','audius:track:2','Artist','Track'),
          ('inGenre','audius:track:1','audius:genre:house','Track','Genre'),
          ('sameAs','audius:artistref:S','audius:user:S','Artist','Artist'),
          ('follows','audius:user:A','audius:user:S','Artist','Artist');
    """)
    db.commit()

    class FakePoint:
        def __init__(self, nid):
            self.payload = {"id": nid, "fields": {"title": nid}, "owner": "0x1", "meta": {}}
            self.vector = [0.1, 0.2]
            self.id = nid

    class FakeQdrant:
        def __init__(self):
            self.asked = []

        def retrieve(self, collection_name, ids, with_payload, with_vectors):
            self.asked.append(ids)
            # Return in REVERSE, so a test that expects edge order proves the route
            # re-orders rather than trusting Qdrant's.
            return [FakePoint(n) for n in reversed(
                ["audius:track:1", "audius:track:2"])]

    fake = FakeQdrant()
    monkeypatch.setattr(server, "_adj", db)
    monkeypatch.setattr(server, "_sovereign", {"audius:user:S"})
    monkeypatch.setattr(server, "qdrant_client", fake)
    monkeypatch.setattr(server, "cfg", argparse.Namespace(collection="t"))
    # _str_to_uuid is irrelevant to these assertions; keep ids readable.
    monkeypatch.setattr(server, "_str_to_uuid", lambda s: s)
    return fake


def test_groups_cover_both_directions(wired):
    out = asyncio.run(server.adjacency(id="audius:track:1", rel=None, dir=None, limit=60))
    got = {(g["rel"], g["dir"]): g["count"] for g in out["groups"]}
    assert got[("created", "in")] == 1, "inbound edges must be reported"
    assert got[("inGenre", "out")] == 1, "outbound edges must be reported"


def test_crosses_marks_a_publisher_boundary(wired):
    """The badge the demo exists to show. If this never fires, cross-publisher rails
    look identical to ordinary ones and the sovereignty story disappears."""
    out = asyncio.run(server.adjacency(id="audius:user:A", rel=None, dir=None, limit=60))
    g = {(x["rel"], x["dir"]): x for x in out["groups"]}
    assert g[("follows", "out")]["crosses"] is True, "A->S crosses into the sovereign side"
    assert g[("created", "out")]["crosses"] is False, "A->own tracks does not cross"


def test_crossing_rails_sort_first(wired):
    out = asyncio.run(server.adjacency(id="audius:user:A", rel=None, dir=None, limit=60))
    assert out["groups"][0]["crosses"] is True, "crossing rails must lead, as in Graph.relations"


def test_neighbours_respect_direction(wired):
    out = asyncio.run(server.adjacency(id="audius:user:A", rel="created", dir="out", limit=60))
    assert [r["id"] for r in out["records"]] == ["audius:track:1", "audius:track:2"]
    # 'in' on the same node/rel must be empty — a swapped direction would silently
    # return the wrong half of the graph.
    empty = asyncio.run(server.adjacency(id="audius:user:A", rel="created", dir="in", limit=60))
    assert empty["records"] == []


def test_neighbours_preserve_edge_order_not_qdrant_order(wired):
    """Qdrant's retrieve makes no ordering promise. Rails would shuffle between loads."""
    out = asyncio.run(server.adjacency(id="audius:user:A", rel="created", dir="out", limit=60))
    assert [r["id"] for r in out["records"]] == ["audius:track:1", "audius:track:2"]


def test_limit_is_applied_in_sql(wired):
    out = asyncio.run(server.adjacency(id="audius:user:A", rel="created", dir="out", limit=1))
    assert len(wired.asked[-1]) == 1, "limit must bound the id list before the fetch"


def test_route_is_501_without_a_db(monkeypatch):
    monkeypatch.setattr(server, "_adj", None)
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.adjacency(id="x", rel=None, dir=None, limit=60))
    assert e.value.status_code == 501
