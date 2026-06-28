"""
osm.py — OpenStreetMap *places* ingest for Fangorn.

Proves the thesis that adding a new domain is a *schema change, not an
architecture change*: this mirrors the music pipelines but emits records under
an `osm_place` schema. The ingest server + app then infer the semantic roles
automatically (title←name, subtitle←category, tags←tags, spatial←lat/lon — no
media → no player).

Two structural ideas:
  1. Place selection is separated from OSM fetching. You pick a *named* place
     ("Eagle River, Wisconsin", "Vilas County, Wisconsin") and the script
     discovers its bounding box via Nominatim — no hardcoded bbox.
  2. We query Overpass for actual entities (restaurants, bars, parks,
     campgrounds, trails, boat launches…) rather than changesets, which is what
     a local-discovery app actually wants.

Output: ./stage_volumes/osm_places.json — a JSON array of
{"name": <place name>, "fields": {...}} records, the same shape the SDK ingest
path consumes.

No third-party deps — stdlib only (urllib + json). Run:
    python osm.py
"""

import os
import json
import time
import urllib.request
import urllib.parse

# ===========================================================================
# CONFIG — pick a *named* place; the bbox is discovered automatically.
# ===========================================================================
CONFIG = {
    "place":        "Eagle River, Wisconsin",
    "output_dir":   "./stage_volumes",
}

# Categories to pull. Each entry is an Overpass (key, value) filter; value=None
# means "any value for this key" (e.g. all tourism=* / leisure=* entities).
CATEGORIES = [
    ("amenity", "restaurant"),
    ("amenity", "bar"),
    ("amenity", "pub"),
    ("amenity", "cafe"),
    ("tourism", None),
    ("leisure", None),
]

USER_AGENT        = "Fangorn-OSM-Pipeline/1.0 (https://fangorn.network)"
NOMINATIM         = "https://nominatim.openstreetmap.org/search"
OVERPASS          = "https://overpass-api.de/api/interpreter"
REQUEST_PAUSE_SEC = 1.0   # be polite to the public APIs


# ===========================================================================
# PLACE SELECTION — geocode a named place to a bounding box via Nominatim.
# ===========================================================================
def lookup_bbox(place: str) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) for a named place."""
    params = {"q": place, "format": "jsonv2", "limit": 1}
    url = f"{NOMINATIM}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())

    if not data:
        raise ValueError(f"Place not found: {place}")

    # Nominatim boundingbox is [south, north, west, east] (strings).
    south, north, west, east = (float(x) for x in data[0]["boundingbox"])
    return (west, south, east, north)


# ===========================================================================
# OSM FETCHING — query Overpass for entities inside the bbox.
# ===========================================================================
def _build_query(bbox: tuple[float, float, float, float]) -> str:
    west, south, east, north = bbox
    # Overpass bbox order is (south, west, north, east).
    area = f"{south},{west},{north},{east}"
    clauses = []
    for key, value in CATEGORIES:
        sel = f'["{key}"]' if value is None else f'["{key}"="{value}"]'
        # node + way so we catch both point POIs and building footprints.
        clauses.append(f"  node{sel}({area});")
        clauses.append(f"  way{sel}({area});")
    body = "\n".join(clauses)
    return f"[out:json][timeout:60];\n(\n{body}\n);\nout center tags;"


def _category_of(tags: dict) -> str:
    for key, value in CATEGORIES:
        if key in tags:
            return tags[key] if value is None else value
    return "place"


def _parse_element(el: dict) -> dict | None:
    tags = el.get("tags", {})
    name = tags.get("name")
    if not name:
        return None  # unnamed POIs aren't useful for discovery

    # node → lat/lon directly; way → Overpass "out center" gives a center point.
    if el.get("type") == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        center = el.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None

    # Surface a compact, human-meaningful tag set for semantic search.
    tag_values = []
    for k in ("amenity", "tourism", "leisure", "cuisine", "shop", "sport"):
        v = tags.get(k)
        if v:
            tag_values.extend(p.strip() for p in v.replace(",", ";").split(";") if p.strip())

    return {
        "name": name,
        "fields": {
            "osm_id":   f"{el.get('type')}/{el.get('id')}",
            "category": _category_of(tags),
            "lat":      lat,
            "lon":      lon,
            "tags":     sorted(set(tag_values)),
        },
    }


def collect_places(bbox: tuple[float, float, float, float]) -> list[dict]:
    query = _build_query(bbox)
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read())

    records: list[dict] = []
    seen: set[str] = set()
    for el in payload.get("elements", []):
        rec = _parse_element(el)
        if rec is None:
            continue
        oid = rec["fields"]["osm_id"]
        if oid in seen:
            continue
        seen.add(oid)
        records.append(rec)
    return records


def main():
    place = CONFIG["place"]
    out_dir = CONFIG["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "osm_places.json")

    print(f"🌍 Geocoding {place!r} via Nominatim…")
    bbox = lookup_bbox(place)
    print(f"   bbox (W,S,E,N) = {bbox}")
    time.sleep(REQUEST_PAUSE_SEC)

    print(f"🔎 Querying Overpass for {len(CATEGORIES)} category filters…")
    records = collect_places(bbox)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Wrote {len(records):,} places → {out_path}")
    if records:
        print("   sample:", records[0]["name"], records[0]["fields"]["category"])


if __name__ == "__main__":
    main()
