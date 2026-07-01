"""Phase 1 — Composed View fusion + identity helpers.

Pure, network-free coverage of the two new pieces:
  * quickbeam._identity — keccak256 / resource_id, locked to fangorn vectors.
  * quickbeam.embeddings._fuse_nodes — union-find over shared global identity.
"""
from quickbeam._identity import keccak256, name_hash, resource_id, norm_hex
from quickbeam.embeddings import _DSU, _fuse_nodes, _alias_index, _resolve_endpoint


# ── identity ────────────────────────────────────────────────────────────────
def test_keccak_known_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"


def test_resource_id_matches_fangorn_sdk():
    # Vector produced by viem keccak256/encodePacked (fangorn DataSourceRegistry).
    owner = "0x00000000000000000000000000000000000000aa"
    schema = "0x" + "11" * 32
    assert name_hash("music.isrc.v1") == \
        "0xe5f5ada2154d0176fed29c17ebf9a5d6060889aaae57039422fc4481ea565e4b"
    expected = "0x734b16754e73124cfe56a3944fc07916b0fa58e13375cb498f14cafafb6d2902"
    assert resource_id(owner, schema, "music.isrc.v1") == expected
    # nameHash shortcut (what the subgraph actually hands us) is equivalent.
    assert resource_id(owner, schema, name_hash("music.isrc.v1"), is_hash=True) == expected


# ── union-find ──────────────────────────────────────────────────────────────
def test_dsu_canonical_root_is_smallest():
    d = _DSU()
    d.union("b", "a")
    d.union("c", "b")
    assert d.find("a") == d.find("c") == "a"


def test_fuse_collapses_nodes_sharing_an_alias():
    # A music track and an artwork node, different sources/Entity URIs, share isrc.
    nodes = {
        "fangorn:0xMUSIC/t1": {
            "id": "t1", "type": "Track", "entityUri": "fangorn:0xMUSIC/t1",
            "aliases": ["isrc:US123"], "fields": {"title": "Song"},
        },
        "fangorn:0xART/a1": {
            "id": "a1", "type": "Track", "entityUri": "fangorn:0xART/a1",
            "aliases": ["isrc:US123"], "fields": {"cover": "art.png"},
        },
    }
    dsu, merged = _fuse_nodes(nodes)
    assert dsu.find("fangorn:0xMUSIC/t1") == dsu.find("fangorn:0xART/a1")
    assert len(merged) == 1
    fused = next(iter(merged.values()))
    # fields from BOTH members are present; aliases unioned.
    assert fused["fields"] == {"title": "Song", "cover": "art.png"}
    assert fused["aliases"] == ["isrc:US123"]
    # canonical key is the lexicographically-smallest member Entity URI.
    assert fused["entityUri"] == "fangorn:0xART/a1"


def test_fuse_keeps_distinct_entities_apart():
    nodes = {
        "fangorn:0xA/1": {"id": "1", "type": "Track", "entityUri": "fangorn:0xA/1",
                          "aliases": ["isrc:AAA"], "fields": {}},
        "fangorn:0xB/2": {"id": "2", "type": "Track", "entityUri": "fangorn:0xB/2",
                          "aliases": ["isrc:BBB"], "fields": {}},
    }
    _dsu, merged = _fuse_nodes(nodes)
    assert len(merged) == 2


def test_fuse_alias_less_nodes_survive():
    nodes = {
        "fangorn:0xA/1": {"id": "1", "type": "Place", "entityUri": "fangorn:0xA/1",
                          "aliases": [], "fields": {"name": "Bar"}},
    }
    _dsu, merged = _fuse_nodes(nodes)
    assert len(merged) == 1
    assert next(iter(merged.values()))["fields"] == {"name": "Bar"}


def test_norm_hex_lowercases():
    assert norm_hex("0xABC") == "0xabc"


# ── Phase 2: linkset sameAs feeds the SAME union-find ─────────────────────────
def _two_unaliased():
    # Two nodes with no shared alias — nothing collapses them on identity alone.
    return {
        "fangorn:0xPLACES/p1": {"id": "p1", "type": "Place", "entityUri": "fangorn:0xPLACES/p1",
                                "aliases": [], "fields": {"name": "Marina Bar"}},
        "fangorn:0xEVENTS/v9": {"id": "v9", "type": "Place", "entityUri": "fangorn:0xEVENTS/v9",
                                "aliases": [], "fields": {"name": "Marina Bar & Grill"}},
    }


def test_sameas_union_merges_otherwise_distinct_nodes():
    nodes = _two_unaliased()
    # Without the assertion they stay apart…
    _d, merged_before = _fuse_nodes(nodes)
    assert len(merged_before) == 2
    # …with an asserted sameAs they collapse.
    pair = ("fangorn:0xPLACES/p1", "fangorn:0xEVENTS/v9")
    dsu, merged = _fuse_nodes(nodes, extra_unions=[pair])
    assert dsu.find(pair[0]) == dsu.find(pair[1])
    assert len(merged) == 1


def test_resolve_endpoint_by_entity_uri_and_alias():
    nodes = {
        "fangorn:0xA/1": {"id": "1", "type": "Track", "entityUri": "fangorn:0xA/1",
                          "aliases": ["isrc:US123"], "fields": {}},
    }
    idx = _alias_index(nodes)
    assert _resolve_endpoint("fangorn:0xA/1", nodes, idx) == "fangorn:0xA/1"   # direct URI
    assert _resolve_endpoint("isrc:US123", nodes, idx) == "fangorn:0xA/1"      # via alias
    assert _resolve_endpoint("isrc:NOPE", nodes, idx) is None                  # outside the view
    assert _resolve_endpoint(None, nodes, idx) is None


def test_alias_index_first_node_wins():
    nodes = {
        "k1": {"aliases": ["isrc:X"], "fields": {}},
        "k2": {"aliases": ["isrc:X"], "fields": {}},
    }
    assert _alias_index(nodes)["isrc:X"] in ("k1", "k2")


def test_namespaced_local_id_is_addressable_without_being_a_fusion_key():
    # An alias-less event whose local id is already namespaced (`tribe:10020845`)
    # must be resolvable as a linkset endpoint (so a `hostedAt` edge can point at it)…
    nodes = {
        "fangorn:0xEV/tribe:10020845": {
            "id": "tribe:10020845", "type": "Event",
            "entityUri": "fangorn:0xEV/tribe:10020845", "aliases": [], "fields": {},
        },
        "fangorn:0xBIZ/g1": {
            "id": "g1", "type": "Business", "entityUri": "fangorn:0xBIZ/g1",
            "aliases": ["gplace:ChIJ_host"], "fields": {},
        },
    }
    idx = _alias_index(nodes)
    assert _resolve_endpoint("tribe:10020845", nodes, idx) == "fangorn:0xEV/tribe:10020845"
    assert _resolve_endpoint("gplace:ChIJ_host", nodes, idx) == "fangorn:0xBIZ/g1"
    # …yet the namespaced local id must NOT feed the union-find: the event and the
    # business it's hostedAt stay TWO entities (a typed edge, not a fusion).
    _d, merged = _fuse_nodes(nodes)
    assert len(merged) == 2


def test_bare_local_id_is_not_indexed_as_alias():
    # A non-namespaced local id (`g1`) must not pollute the endpoint index.
    nodes = {"fangorn:0xBIZ/g1": {"id": "g1", "type": "Business",
                                  "entityUri": "fangorn:0xBIZ/g1", "aliases": [], "fields": {}}}
    assert _resolve_endpoint("g1", nodes, _alias_index(nodes)) is None
