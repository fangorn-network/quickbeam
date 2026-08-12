"""Tests for the pure core: build_graph, the A/B partition, and the linkset.

No network — the fixture is a hand-built miniature of a real crawl. The invariant
that actually matters here is that **no published graph contains a dangling edge**:
the CDN joins records and edges by id, so an endpoint that isn't in the same graph
is a dead end in the served demo.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from quickbeam_audius.source import (  # noqa: E402
    build_graph, cross_edges, partition, stub_name)
from quickbeam_audius.crawl import artist_name, playlist_name, track_name  # noqa: E402
from quickbeam_audius.link import _key_from_name, to_cid_space  # noqa: E402

FOC, OTH = "FOC01", "OTH01"


def _u(i, handle, **kw):
    return {"id": i, "handle": handle, "name": handle.title(),
            "bio": f"bio of {handle}", "follower_count": 100, **kw}


def _t(i, owner_handle, owner_id, **kw):
    return {"id": i, "title": f"Track {i}", "user": _u(owner_id, owner_handle),
            "genre": "House", "mood": "Cool", "tags": "uk,garage",
            "duration": 200, "play_count": 10, "cover_art_sizes": f"cid-{i}", **kw}


def _p(i, owner_id, owner_handle, is_album=False, **kw):
    return {"id": i, "playlist_name": f"List {i}", "is_album": is_album,
            "user": _u(owner_id, owner_handle), "track_count": 2, **kw}


def _rel(rel, frm, to, ft, tt, **f):
    r = {"type": "rel", "rel": rel, "from": frm, "to": to, "fromType": ft, "toType": tt}
    if f:
        r["fields"] = f
    return r


def records():
    """Focus artist FOC owns FT1/FT2 and album AL1. OTH owns OT1 and playlist PL1 —
    and PL1 contains a FOCUS track, which is the cross-graph edge the demo turns on."""
    return [
        {"type": "artist", "id": FOC, "data": _u(FOC, "disclosure")},
        {"type": "artist", "id": OTH, "data": _u(OTH, "other")},
        {"type": "track", "id": "FT1", "data": _t("FT1", "disclosure", FOC)},
        {"type": "track", "id": "FT2", "data": _t("FT2", "disclosure", FOC,
                                                  genre="Techno", mood="Dark")},
        # Compound genre, and it OVERLAPS the focus artist's — which is what lets
        # the two graphs converge on a shared vocabulary vertex.
        {"type": "track", "id": "OT1", "data": _t("OT1", "other", OTH,
                                                  genre="House, Bass", tags="bass")},
        {"type": "playlist", "id": "PL1", "data": _p("PL1", OTH, "other")},
        {"type": "playlist", "id": "AL1", "data": _p("AL1", FOC, "disclosure", True)},
        _rel("created", artist_name(FOC), track_name("FT1"), "Artist", "Track"),
        _rel("created", artist_name(FOC), track_name("FT2"), "Artist", "Track"),
        _rel("created", artist_name(OTH), track_name("OT1"), "Artist", "Track"),
        _rel("released", artist_name(FOC), playlist_name("AL1"), "Artist", "Playlist"),
        _rel("curated", artist_name(OTH), playlist_name("PL1"), "Artist", "Playlist"),
        _rel("contains", playlist_name("AL1"), track_name("FT1"), "Playlist", "Track"),
        _rel("contains", playlist_name("PL1"), track_name("OT1"), "Playlist", "Track"),
        # THE cross-graph edge: a platform playlist holding the artist's own track.
        _rel("contains", playlist_name("PL1"), track_name("FT1"), "Playlist", "Track"),
        _rel("follows", artist_name(OTH), artist_name(FOC), "Artist", "Artist"),
        _rel("relatedTo", artist_name(FOC), artist_name(OTH), "Artist", "Artist"),
        _rel("supports", artist_name(OTH), artist_name(FOC), "Artist", "Artist",
             amount="23000000000000000000", rank=1),
        _rel("reposted", artist_name(OTH), track_name("FT2"), "Artist", "Track"),
        _rel("favorited", artist_name(OTH), track_name("FT1"), "Artist", "Track"),
        _rel("remixOf", track_name("FT2"), track_name("OT1"), "Track", "Track"),
        _rel("hasStem", track_name("OT1"), track_name("FT1"), "Track", "Track"),
    ]


def _names(nodes):
    return {n["name"] for lst in nodes.values() for n in lst}


def _assert_closed(nodes, edges, label):
    """No edge may point outside its own graph."""
    present = _names(nodes)
    dangling = [e for e in edges if e["from"] not in present or e["to"] not in present]
    assert not dangling, f"{label}: {len(dangling)} dangling edge(s), e.g. {dangling[:3]}"


# -- build_graph -------------------------------------------------------------
def test_build_graph_shapes_all_entity_types():
    nodes, edges = build_graph(records())
    assert set(nodes) == {"Artist", "Track", "Playlist", "Genre", "Mood", "Tag"}
    assert len(nodes["Track"]) == 3
    assert len(nodes["Artist"]) == 2
    _assert_closed(nodes, edges, "full")


def test_derived_vocabulary_edges():
    nodes, edges = build_graph(records())
    rels = {e["rel"] for e in edges}
    for r in ("inGenre", "hasMood", "taggedWith", "worksIn"):
        assert r in rels, f"missing derived relation {r}"
    # Compound genres split: "House" and "Techno" and "Bass" are separate nodes.
    assert {n["fields"]["title"] for n in nodes["Genre"]} == {"House", "Techno", "Bass"}


def test_all_crawled_relations_survive():
    _, edges = build_graph(records())
    rels = {e["rel"] for e in edges}
    expected = {"created", "released", "curated", "contains", "follows", "relatedTo",
                "supports", "reposted", "favorited", "remixOf", "hasStem",
                "inGenre", "hasMood", "taggedWith", "worksIn"}
    assert expected <= rels, f"missing {expected - rels}"


def test_relation_fields_survive():
    """The $AUDIO tip amount is the demo's one real economic edge — it must not be
    dropped on the way through."""
    _, edges = build_graph(records())
    sup = [e for e in edges if e["rel"] == "supports"]
    assert sup and sup[0]["fields"]["amount"] == "23000000000000000000"


def test_build_graph_is_deterministic():
    """Content addressing means byte-identical re-runs, or an unchanged commit
    re-uploads the whole graph."""
    a = json.dumps(build_graph(records()), sort_keys=True)
    b = json.dumps(build_graph(records()), sort_keys=True)
    assert a == b


# -- the A/B partition -------------------------------------------------------
def test_side_b_is_only_the_artists_own_content():
    recs = partition(records(), FOC, "B")
    nodes, edges = build_graph(recs)
    assert _names(nodes) >= {artist_name(FOC), track_name("FT1"), track_name("FT2"),
                             playlist_name("AL1")}
    assert track_name("OT1") not in _names(nodes)
    assert playlist_name("PL1") not in _names(nodes)
    assert artist_name(OTH) not in _names(nodes)
    _assert_closed(nodes, edges, "side B")


def test_side_a_excludes_the_artists_catalog_but_keeps_a_stub():
    recs = partition(records(), FOC, "A")
    nodes, edges = build_graph(recs)
    names = _names(nodes)
    assert stub_name(FOC) in names, "platform needs a reference to the artist"
    assert artist_name(FOC) not in names, "the artist's real node belongs to them"
    assert track_name("FT1") not in names and track_name("FT2") not in names
    assert playlist_name("AL1") not in names
    assert track_name("OT1") in names and playlist_name("PL1") in names
    _assert_closed(nodes, edges, "side A")


def test_artwork_travels_as_a_cid_not_a_url():
    """The API returns a different content-node host per response for byte-identical
    bytes, so a URL would bake in a random mirror."""
    nodes, _ = build_graph(records())
    t = next(n for n in nodes["Track"] if n["name"] == track_name("FT1"))
    assert t["fields"]["artworkCid"] == "cid-FT1"
    assert "http" not in t["fields"]["artworkCid"]


def test_stub_carries_no_artwork():
    """The platform holds a reference, not the artist's images — so the stub renders
    a placeholder. That gap is the point, not a rendering bug."""
    nodes, _ = build_graph(partition(records(), FOC, "A"))
    stub = next(n for n in nodes["Artist"] if n["name"] == stub_name(FOC))
    assert "avatarCid" not in stub["fields"]


def test_stub_is_minimal():
    """The platform holds a reference, not a profile."""
    nodes, _ = build_graph(partition(records(), FOC, "A"))
    stub = next(n for n in nodes["Artist"] if n["name"] == stub_name(FOC))
    assert stub["fields"]["isReference"] is True
    assert "bio" not in stub["fields"] and "followerCount" not in stub["fields"]


def test_edges_onto_the_artist_node_are_rewritten_onto_the_stub():
    """`other follows disclosure` is the platform's own knowledge — it stays in A,
    pointed at the stub, rather than dangling or leaking into the linkset."""
    _, edges = build_graph(partition(records(), FOC, "A"))
    follows = [e for e in edges if e["rel"] == "follows"]
    assert follows and follows[0]["to"] == stub_name(FOC)


def test_vocabulary_converges_across_both_graphs():
    """Both sides derive `Genre: House` independently. Identical payload bytes mean
    Fangorn gives both publishers the SAME CID — the graphs merge at that vertex
    with no linkset entry and no coordination."""
    a_nodes, _ = build_graph(partition(records(), FOC, "A"))
    b_nodes, _ = build_graph(partition(records(), FOC, "B"))
    a = next(n for n in a_nodes["Genre"] if n["fields"]["title"] == "House")
    b = next(n for n in b_nodes["Genre"] if n["fields"]["title"] == "House")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# -- the linkset -------------------------------------------------------------
def test_linkset_carries_the_cross_graph_edges():
    links = cross_edges(records(), FOC)
    pairs = {(e["rel"], e["from"], e["to"]) for e in links}
    assert ("sameAs", stub_name(FOC), artist_name(FOC)) in pairs
    # The money shot: a platform playlist containing the artist's own track.
    assert ("contains", playlist_name("PL1"), track_name("FT1")) in pairs
    assert ("remixOf", track_name("FT2"), track_name("OT1")) in pairs
    assert ("favorited", artist_name(OTH), track_name("FT1")) in pairs


def test_linkset_excludes_edges_either_side_can_publish_alone():
    links = cross_edges(records(), FOC)
    pairs = {(e["rel"], e["from"], e["to"]) for e in links}
    assert ("contains", playlist_name("PL1"), track_name("OT1")) not in pairs  # A only
    assert ("contains", playlist_name("AL1"), track_name("FT1")) not in pairs  # B only
    assert ("follows", artist_name(OTH), artist_name(FOC)) not in pairs  # absorbed by stub


def test_partition_plus_linkset_loses_no_edge():
    """A + B + linkset must account for every edge the full graph has. This is the
    whole claim: splitting the graph across two publishers destroys nothing."""
    _, full = build_graph(records())
    _, a = build_graph(partition(records(), FOC, "A"))
    _, b = build_graph(partition(records(), FOC, "B"))
    links = cross_edges(records(), FOC)

    def keys(edges, unstub=True):
        out = set()
        for e in edges:
            f = artist_name(FOC) if unstub and e["from"] == stub_name(FOC) else e["from"]
            t = artist_name(FOC) if unstub and e["to"] == stub_name(FOC) else e["to"]
            out.add((e["rel"], f, t))
        return out

    recovered = keys(a) | keys(b) | keys(links)
    missing = keys(full) - recovered
    assert not missing, f"edges lost in the split: {missing}"


# -- id spaces ---------------------------------------------------------------
def test_key_from_name_separates_stub_from_real_artist():
    assert _key_from_name(stub_name(FOC)) != _key_from_name(artist_name(FOC))
    assert _key_from_name("audius:track:FT1") == ("Track", "FT1", False)
    assert _key_from_name("not-an-audius-name") is None


def test_cid_rewrite_reports_unresolved_endpoints():
    edges = [{"rel": "sameAs", "from": stub_name(FOC), "to": artist_name(FOC),
              "fromType": "Artist", "toType": "Artist"}]
    idx = {("Artist", FOC, True): "bafyStub", ("Artist", FOC, False): "bafyReal"}
    ok, bad = to_cid_space(edges, idx)
    assert not bad and ok[0]["from"] == "bafyStub" and ok[0]["to"] == "bafyReal"

    ok2, bad2 = to_cid_space(edges, {("Artist", FOC, True): "bafyStub"})
    assert not ok2 and bad2[0]["missingTo"] == artist_name(FOC)
