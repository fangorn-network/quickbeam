"""schemagen identity inference — an alias is emitted ONLY for the node's own id.

Regression for the over-merge bug: a field that merely *points at* another node
(a Review's `businessId`, an Event's `hostBusinessId`) must get NEITHER `@id` nor an
alias. An alias is a fusion join key, so a foreign-key alias makes a View's union-find
collapse every child onto its parent. Only an own-id field (Business `placeId`, whose
value equals the record's local id) becomes an alias and is promoted to `@id`.
"""
from quickbeam.pipelines.fangorn_schema import _infer_identity


def test_promotes_own_id_field():
    fv = {"placeId": ["ChIJ-3PfJ9_JVU0Rj8qW3Gt6tFw", "ChIJabcdefghij0", "ChIJ0123456789a"]}
    ident = _infer_identity(fv, id_fields=frozenset({"placeId"}))
    assert ident == {"aliases": {"gplace": "placeId"}, "@id": "placeId"}


def test_foreign_key_gets_no_alias():
    # businessId values are Place IDs (gplace shape) but it's a foreign key, so
    # id_fields does NOT contain it → no alias at all, no identity.
    fv = {"businessId": ["ChIJ-3PfJ9_JVU0Rj8qW3Gt6tFw", "ChIJabcdefghij0"]}
    assert _infer_identity(fv, id_fields=frozenset()) is None


def test_own_id_kept_foreign_key_dropped_together():
    # A node with its own placeId AND a foreign hostBusinessId keeps only placeId.
    fv = {
        "placeId": ["ChIJ-3PfJ9_JVU0Rj8qW3Gt6tFw", "ChIJabcdefghij0"],
        "hostBusinessId": ["ChIJzzzzzzzzzzzzzzzzzzzz0", "ChIJyyyyyyyyyyyyyyyyyyyy0"],
    }
    ident = _infer_identity(fv, id_fields=frozenset({"placeId"}))
    assert ident == {"aliases": {"gplace": "placeId"}, "@id": "placeId"}


def test_osm_own_id_promotes():
    fv = {"osmId": ["node/4987275152", "way/123", "relation/456"]}
    ident = _infer_identity(fv, id_fields=frozenset({"osmId"}))
    assert ident == {"aliases": {"osm": "osmId"}, "@id": "osmId"}


def test_gplace_wins_at_id_over_osm_when_both_are_own_id():
    fv = {
        "placeId": ["ChIJ-3PfJ9_JVU0Rj8qW3Gt6tFw", "ChIJabcdefghij0"],
        "osmId": ["node/1", "way/2"],
    }
    ident = _infer_identity(fv, id_fields=frozenset({"placeId", "osmId"}))
    assert ident["@id"] == "placeId"  # PROMOTE_ID order: gplace before osm
    assert ident["aliases"] == {"gplace": "placeId", "osm": "osmId"}


def test_no_recognized_namespace_returns_none():
    assert _infer_identity({"title": ["Marina Bar", "Shotskis"]}, id_fields=frozenset({"title"})) is None
