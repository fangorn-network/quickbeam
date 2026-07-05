"""
test_places.py — the PlacesSource pure shaper + graph builder. No Postgres, no
network: hand-built Google Places payloads exercise shape → build_graph, the
node/edge contract downstream depends on.
"""
from quickbeam.ingest.scrapers.places import (
    PlacesSource,
    shape_business,
    shape_review,
)

_PAYLOAD = {
    "id": "g:1", "displayName": {"text": "Riverside Supper Club"},
    "primaryType": "restaurant", "primaryTypeDisplayName": {"text": "Supper Club"},
    "types": ["restaurant", "bar"], "formattedAddress": "12 Main St, Eagle River, WI",
    "location": {"latitude": 45.9200, "longitude": -89.2500},
    "rating": 4.6, "userRatingCount": 210, "priceLevel": "PRICE_LEVEL_MODERATE",
    "servesBeer": True, "liveMusic": True,
    "editorialSummary": {"text": "Lakeside supper club with walleye"},
    "addressComponents": [{"types": ["locality"], "longText": "Eagle River"},
                          {"types": ["administrative_area_level_1"], "shortText": "WI"}],
    "reviews": [{"authorAttribution": {"displayName": "Jane D", "uri": "http://g/jane"},
                 "text": {"text": "Great walleye"}, "rating": 5,
                 "relativePublishTimeDescription": "a month ago"}],
}


def test_shape_business_blurb_price_and_amenities():
    f = shape_business("g:1", _PAYLOAD, is_anchor=True)
    assert f["entityType"] == "Business" and f["placeId"] == "g:1"
    assert f["priceLevel"] == "$$"                      # PRICE_LEVEL_MODERATE
    assert f["locality"] == "Eagle River, WI"
    assert f["isAnchor"] is True
    assert set(f["amenities"]) == {"serves beer", "live music"}
    t = f["text"]
    assert "Riverside Supper Club — Supper Club in Eagle River, WI" in t
    assert "Lakeside supper club with walleye" in t
    assert "Rated 4.6/5 from 210 reviews" in t


def test_shape_review_and_reviewer_keys():
    node, reviewer, akey = shape_review("g:1", "Riverside Supper Club", 0,
                                        _PAYLOAD["reviews"][0])
    assert node["reviewId"] == "g:1:0"                  # <place_id>:<idx>
    assert node["rating"] == 5 and node["author"] == "Jane D"
    assert reviewer["reviewerId"] == akey and akey.startswith("rev-")   # stable id
    assert "Review of Riverside Supper Club (5/5) by Jane D" in node["text"]


def test_build_graph_nodes_hubs_edges():
    src = PlacesSource()
    src._near_radius = 0.0
    rows = [{"place_id": "g:1", "is_anchor": True, "payload": _PAYLOAD}]
    nodes, edges = src.build_graph(rows)
    assert {n["name"] for n in nodes["Business"]} == {"g:1"}
    assert {n["name"] for n in nodes["Review"]} == {"g:1:0"}
    assert {n["name"] for n in nodes["Category"]} == {"restaurant", "bar"}
    assert len(nodes["Locality"]) == 1 and len(nodes["Reviewer"]) == 1
    rels = {(e["from"], e["rel"], e["to"]) for e in edges}
    assert ("g:1", "hasReview", "g:1:0") in rels
    assert ("g:1:0", "byAuthor", nodes["Reviewer"][0]["name"]) in rels
    assert ("g:1", "inCategory", "restaurant") in rels
    assert ("g:1", "locatedIn", "eagle-river-wi") in rels


def test_near_edges_bidirectional():
    src = PlacesSource()
    src._near_radius = 1500.0
    rows = [
        {"place_id": "g:1", "is_anchor": False, "payload": _PAYLOAD},
        {"place_id": "g:2", "is_anchor": False,
         "payload": {"id": "g:2", "displayName": {"text": "Trestle Cafe"},
                     "types": ["cafe"], "location": {"latitude": 45.9205, "longitude": -89.2505}}},
    ]
    _nodes, edges = src.build_graph(rows)
    near = {(e["from"], e["to"]) for e in edges if e["rel"] == "near"}
    assert ("g:1", "g:2") in near and ("g:2", "g:1") in near


def test_batch_source_never_advances_cursor():
    assert PlacesSource().next_cursor([], 5) == 5
