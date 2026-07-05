"""
test_osm.py — the OsmSource pure shaper + graph builder. No Overpass, no Wikidata:
hand-built OSM element records exercise shape_node / categories / locality hubs /
build_graph (incl. `near` edges), the node/edge contract downstream depends on.
"""
from quickbeam.ingest.scrapers.osm import (
    OsmSource,
    _category_values,
    _image_from_tags,
    _locality,
    shape_node,
)


# ── Category extraction ───────────────────────────────────────────────────────
def test_category_values_primary_plus_secondary_dedup():
    tags = {"amenity": "restaurant", "cuisine": "pizza;pizza,burgers", "sport": "bowls"}
    # primary (amenity) first, then cuisine tokens split on ; and , (deduped), then sport
    assert _category_values(tags) == ["restaurant", "pizza", "burgers", "bowls"]


def test_locality_falls_back_to_queried_place():
    # addr:* wins; the spelled-out --place region is normalized to its USPS code.
    assert _locality({"addr:city": "Eagle River", "addr:state": "WI"}, None, None) \
        == ("eagle-river-wi", "Eagle River, WI", "WI")
    assert _locality({}, "Eagle River", "Wisconsin") \
        == ("eagle-river-wi", "Eagle River, WI", "WI")
    assert _locality({}, None, None) == (None, None, None)


# ── Node shaping per type ─────────────────────────────────────────────────────
def test_image_from_tags_resolves_commons_thumb():
    # a Commons file tag → a free, no-key thumbnail URL (zero network); `read` passes
    # the result into shape_node as image_url.
    url = _image_from_tags({"wikimedia_commons": "File:Riverside.jpg"})
    assert url.startswith("https://commons.wikimedia.org/wiki/Special:FilePath/")
    assert _image_from_tags({}) is None


def test_shape_business_blurb_and_image_url():
    tags = {"name": "Riverside Supper Club", "amenity": "restaurant",
            "cuisine": "american;steak"}
    f = shape_node("node/1", "Business", tags, 45.92, -89.25, "Eagle River, WI",
                   image_url="http://img/rsc.jpg")
    assert f["entityType"] == "Business" and f["title"] == "Riverside Supper Club"
    assert f["primaryType"] == "Restaurant"
    assert f["coordinates"] == "45.92,-89.25"
    assert "Cuisine: american, steak" in f["text"]
    assert f["imageUrl"] == "http://img/rsc.jpg"


def test_shape_trail_carries_route_and_distance():
    tags = {"name": "Nicolet Trail", "route": "snowmobile", "distance": "42 km",
            "surface": "snow"}
    f = shape_node("relation/2", "Trail", tags, 45.9, -89.2, "Eagle River, WI")
    assert f["routeType"] == "snowmobile" and f["distance"] == "42 km"
    assert "Snowmobile trail" in f["text"]


def test_shape_lake_and_landmark():
    lake = shape_node("way/3", "Lake", {"name": "Eagle Lake", "natural": "water",
                                        "water": "lake"}, 45.9, -89.2, None)
    assert lake["entityType"] == "Lake" and lake["waterType"] == "lake"
    assert lake["text"] == "Eagle Lake — lake"
    lm = shape_node("node/4", "Landmark", {"name": "Museum", "tourism": "museum"},
                    45.9, -89.2, "Eagle River, WI")
    assert lm["landmarkType"] == "museum" and "Museum — Museum" in lm["text"]


# ── The graph builder ─────────────────────────────────────────────────────────
def _records():
    return [
        {"osm_id": "node/1", "node_type": "Business",
         "tags": {"name": "Supper Club", "amenity": "restaurant", "addr:city": "Eagle River",
                  "addr:state": "WI"}, "lat": 45.9200, "lon": -89.2500, "image_url": None},
        {"osm_id": "way/3", "node_type": "Lake",
         "tags": {"name": "Eagle Lake", "natural": "water", "addr:city": "Eagle River",
                  "addr:state": "WI"}, "lat": 45.9205, "lon": -89.2505, "image_url": None},
    ]


def test_build_graph_hubs_and_edges():
    src = OsmSource()
    src._default_city = src._default_region = None
    src._near_radius = 0.0
    nodes, edges = src.build_graph(_records())
    assert {n["name"] for n in nodes["Business"]} == {"node/1"}
    assert {n["name"] for n in nodes["Lake"]} == {"way/3"}
    # one shared Locality hub (both in Eagle River), two Category hubs (restaurant, water)
    assert len(nodes["Locality"]) == 1
    assert {n["name"] for n in nodes["Category"]} == {"restaurant", "water"}
    rels = {(e["from"], e["rel"], e["to"]) for e in edges}
    assert ("node/1", "locatedIn", "eagle-river-wi") in rels
    assert ("node/1", "inCategory", "restaurant") in rels


def test_build_graph_near_edges_are_bidirectional():
    src = OsmSource()
    src._default_city = src._default_region = None
    src._near_radius = 1000.0            # the two records are ~65m apart
    _nodes, edges = src.build_graph(_records())
    near = {(e["from"], e["to"]) for e in edges if e["rel"] == "near"}
    assert ("node/1", "way/3") in near and ("way/3", "node/1") in near
    # near edges carry the rounded distance in metres
    assert all("meters" in e for e in edges if e["rel"] == "near")


def test_batch_source_never_advances_cursor():
    assert OsmSource().next_cursor(_records(), 7) == 7
