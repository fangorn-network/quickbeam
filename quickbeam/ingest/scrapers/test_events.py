"""
test_events.py — the EventsSource pure shaper + graph builder. No Postgres, no
network: hand-built payloads exercise normalize → shape → build_graph, the
event-dict contract everything downstream depends on.
"""
from quickbeam.ingest.scrapers.events import (
    EventsSource,
    normalize_eventbrite,
    normalize_tribe,
    shape_event,
    shape_organizer,
)

# One Eventbrite (showmore shape) + one Tribe payload — enough to hit both
# normalizers and every price/venue/organizer branch.
_EB = {
    "id": "E1", "name": {"text": "Jazz Night"}, "url": "http://tix/e1",
    "start": {"local": "2030-06-01T19:30:00", "timezone": "America/Chicago"},
    "end": {"local": "2030-06-01T22:00:00"},
    "ticket_availability": {"is_free": False,
                            "minimum_ticket_price": {"major_value": "15.0"},
                            "maximum_ticket_price": {"major_value": "25.0"}},
    "currency": "USD", "category": {"name": "Music"},
    "summary": "<p>Live jazz &amp; drinks</p>",
    "_venue": {"name": "The Blue Room", "address": {
        "localized_address_display": "12 Main St", "latitude": "45.9200",
        "longitude": "-89.2500", "city": "Eagle River", "region": "WI"}},
    "_organizer": {"id": "O1", "name": "Riverside Presents",
                   "website": "http://riverside", "bio": "<p>Local promoter</p>"},
    "image": {"url": "http://img/e1.jpg"},
}
_TRIBE = {
    "id": "T1", "title": "Farmers Market", "url": "http://tix/t1",
    "start_date": "2020-05-02 08:00:00", "end_date": "2020-05-02 12:00:00",
    "cost": "Free", "venue": {"venue": "Town Square", "geo_lat": "45.9210",
                              "geo_lng": "-89.2490", "city": "Eagle River", "state": "WI"},
    "organizer": [{"id": "O2", "organizer": "City of Eagle River"}],
    "categories": [{"name": "Community"}], "excerpt": "Weekly market",
}


# ── Normalizers ───────────────────────────────────────────────────────────────
def test_normalize_eventbrite_flattens_both_shapes():
    ev = normalize_eventbrite(_EB)
    assert ev["source"] == "eventbrite"
    assert ev["event_id"] == "E1"
    assert ev["title"] == "Jazz Night"
    assert ev["start_date"] == "2030-06-01" and ev["start_time"] == "19:30"
    assert ev["price_min"] == 15.0 and ev["price_max"] == 25.0 and ev["is_free"] is False
    assert (ev["lat"], ev["lng"]) == (45.92, -89.25)
    assert ev["organizer_name"] == "Riverside Presents"
    assert ev["organizer_bio"] == "Local promoter"          # HTML stripped
    assert ev["summary"] == "Live jazz & drinks"            # entities decoded
    assert ev["categories"] == ["Music"]


def test_normalize_tribe_free_event():
    ev = normalize_tribe(_TRIBE)
    assert ev["source"] == "tribe"
    assert ev["is_free"] is True and ev["price_display"] == "Free"
    assert ev["start_date"] == "2020-05-02" and ev["start_time"] == "08:00"
    assert ev["organizer_name"] == "City of Eagle River"
    assert ev["categories"] == ["Community"]


# ── Node shaping ──────────────────────────────────────────────────────────────
def test_shape_event_blurb_and_fields():
    ev = normalize_eventbrite(_EB)
    node = shape_event(ev)
    assert node["entityType"] == "Event"
    assert node["eventId"] == "E1"
    assert node["coordinates"] == "45.92,-89.25"
    assert node["locality"] == "Eagle River, WI"
    # the embedded blurb weaves venue / organizer / date / summary together
    text = node["text"]
    assert "Jazz Night" in text and "The Blue Room" in text
    assert "hosted by Riverside Presents" in text
    assert "Live jazz & drinks" in text


def test_shape_event_marks_past_events():
    past = shape_event(normalize_tribe(_TRIBE))       # 2020 → past
    assert past["isPast"] is True
    assert "past event" in past["text"]


def test_shape_organizer_carries_bio():
    org = shape_organizer(normalize_eventbrite(_EB))
    assert org["entityType"] == "Organizer"
    assert org["title"] == "Riverside Presents"
    assert "Local promoter" in org["text"]


# ── The graph builder + merge link ────────────────────────────────────────────
def _events():
    return [normalize_eventbrite(_EB), normalize_tribe(_TRIBE)]


def test_build_graph_nodes_edges_and_dedup():
    src = EventsSource()
    src._businesses, src._radius = [], 120.0
    nodes, edges = src.build_graph(_events())
    assert {n["name"] for n in nodes["Event"]} == {"eventbrite:E1", "tribe:T1"}
    # two distinct organizers, one shared locality (both in Eagle River, WI)
    assert len(nodes["Organizer"]) == 2
    assert len(nodes["Locality"]) == 1
    rels = {(e["from"], e["rel"], e["toType"]) for e in edges}
    assert ("eventbrite:E1", "hostedBy", "Organizer") in rels
    assert ("eventbrite:E1", "inCategory", "Category") in rels
    assert ("tribe:T1", "locatedIn", "Locality") in rels


def test_build_graph_hosted_at_merge_link():
    # A business at the Jazz Night venue coords → the event links to it (and back).
    src = EventsSource()
    src._businesses = [{"place_id": "g:biz:blue", "title": "the blue room",
                        "title_display": "The Blue Room", "coords": (45.9200, -89.2500)}]
    src._radius = 120.0
    nodes, edges = src.build_graph(_events())
    ev_node = next(n for n in nodes["Event"] if n["name"] == "eventbrite:E1")
    assert ev_node["fields"]["hostBusinessId"] == "g:biz:blue"
    assert ev_node["fields"]["hostBusinessName"] == "The Blue Room"
    rels = {(e["rel"], e["from"], e["to"]) for e in edges}
    assert ("hostedAt", "eventbrite:E1", "g:biz:blue") in rels
    assert ("hostsEvent", "g:biz:blue", "eventbrite:E1") in rels


def test_batch_source_never_advances_cursor():
    assert EventsSource().next_cursor(_events(), 0) == 0
    assert EventsSource().next_cursor(_events(), 42) == 42
