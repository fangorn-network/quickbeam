#!/usr/bin/env python3
"""
OsmSource — OpenStreetMap (Overpass) → Fangorn graph. The harness `Source` port of
`pipelines/osm.py` (no key, no card, no DB — the free, ToS-clean spatial backbone).

A BATCH source: a single fetch + shape for a named place, no live tail — so
`next_cursor` never advances and the harness's checkpoint/watch/accumulate machinery
no-ops. The read half does all network IO (Overpass per layer + Wikidata images) and
the cross-layer dedup (which depends on layer order); `build_graph` is the pure shape
+ category/locality hubs + optional O(n²) `near` edges.

    quickbeam data osm --place "Eagle River, Wisconsin" --volume 3

Output (matches the old pipeline so schemagen/build are unchanged):

    volume_<n>_osm_businesses.json / _trails / _lakes / _landmarks
    volume_<n>_osm_categories.json / _localities   (shared hubs)
    volume_<n>_osm_edges.json                        (inCategory, locatedIn, near)

Extending it: add a tuple to LAYERS (selector → node type). schemagen infers the
union schema automatically, so a new OSM category/type needs no downstream change.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import time
import urllib.parse
import urllib.request
from typing import Optional

from .harness import run_source
from .source import SourceBase

# ============================================================================
# CONFIG
# ============================================================================
OUTPUT_DIR = "./stage_volumes"
SCHEMA_VERSION = 1
USER_AGENT = "Fangorn-OSM-Pipeline/2.0 (https://fangorn.network)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
REQUEST_PAUSE_SEC = 1.0
MAX_RETRIES = 5


# ============================================================================
# LAYERS — the declarative ingest registry.
#
# Each layer is (label, node_type, selectors), where selectors is a list of
# (key, value) Overpass tag filters (value=None ⇒ match any value of that key).
# All selectors in a layer are fetched in ONE Overpass query (a union), then the
# elements are shaped into `node_type`. Layers run top-to-bottom and an element is
# claimed by the FIRST layer that returns it (dedup by osm_id), so order layers
# most-specific → most-generic.
# ============================================================================
LAYERS: list[tuple[str, str, list[tuple[str, Optional[str]]]]] = [
    ("food & drink", "Business", [
        ("amenity", "restaurant"), ("amenity", "bar"), ("amenity", "pub"),
        ("amenity", "cafe"), ("amenity", "fast_food"), ("amenity", "biergarten"),
        ("amenity", "ice_cream"), ("amenity", "food_court"), ("amenity", "nightclub"),
        ("amenity", "winery"), ("amenity", "brewery"),
    ]),
    ("lodging", "Business", [
        ("tourism", "hotel"), ("tourism", "motel"), ("tourism", "guest_house"),
        ("tourism", "hostel"), ("tourism", "chalet"), ("tourism", "apartment"),
        ("tourism", "resort"), ("tourism", "camp_site"), ("tourism", "caravan_site"),
        ("tourism", "wilderness_hut"), ("tourism", "alpine_hut"),
    ]),
    ("recreation", "Business", [
        ("leisure", "sports_centre"), ("leisure", "golf_course"),
        ("leisure", "fitness_centre"), ("leisure", "horse_riding"),
        ("leisure", "bowling_alley"), ("leisure", "water_park"),
        ("leisure", "amusement_arcade"), ("leisure", "ice_rink"),
    ]),
    ("shops", "Business", [("shop", None)]),
    ("services", "Business", [
        ("amenity", "fuel"), ("amenity", "pharmacy"), ("amenity", "bank"),
        ("amenity", "marketplace"), ("amenity", "cinema"), ("amenity", "theatre"),
        ("amenity", "fitness_centre"), ("amenity", "boat_rental"),
        ("amenity", "car_rental"), ("amenity", "clinic"), ("amenity", "hospital"),
    ]),
    ("trails", "Trail", [
        ("route", "hiking"), ("route", "foot"), ("route", "bicycle"),
        ("route", "mtb"), ("route", "snowmobile"), ("route", "ski"),
        ("route", "canoe"), ("route", "piste"), ("piste:type", None),
    ]),
    ("water bodies", "Lake", [
        ("natural", "water"), ("natural", "bay"), ("water", None),
    ]),
    ("natural features", "Landmark", [
        ("natural", "peak"), ("natural", "beach"), ("natural", "wood"),
        ("natural", "cliff"), ("natural", "wetland"), ("natural", "spring"),
        ("natural", "cave_entrance"),
    ]),
    ("outdoors", "Landmark", [
        ("leisure", "park"), ("leisure", "nature_reserve"), ("leisure", "garden"),
        ("leisure", "marina"), ("leisure", "slipway"), ("leisure", "beach_resort"),
        ("leisure", "swimming_area"), ("leisure", "fishing"), ("leisure", "dog_park"),
        ("leisure", "picnic_table"), ("leisure", "playground"),
        ("boundary", "protected_area"), ("boundary", "national_park"),
    ]),
    ("attractions", "Landmark", [
        ("tourism", "attraction"), ("tourism", "viewpoint"), ("tourism", "museum"),
        ("tourism", "gallery"), ("tourism", "artwork"), ("tourism", "theme_park"),
        ("tourism", "zoo"), ("tourism", "picnic_site"), ("tourism", "information"),
    ]),
    ("historic", "Landmark", [("historic", None)]),
]

PRIMARY_CATEGORY_KEYS = [
    "amenity", "shop", "tourism", "leisure", "route", "piste:type",
    "natural", "water", "historic", "boundary", "man_made",
]
SECONDARY_CATEGORY_KEYS = ["cuisine", "sport"]

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


# ============================================================================
# HELPERS
# ============================================================================
def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items()
            if v is not None and v != "" and v != [] and v != {}}


def _slug(*parts: str) -> str:
    s = "-".join(p for p in parts if p)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "unknown"


def _norm_region(region: Optional[str]) -> Optional[str]:
    if not region:
        return region
    return US_STATES.get(region.strip().lower(), region.strip())


def _haversine_m(a: tuple, b: tuple) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _humanize(value: str) -> str:
    return value.replace("_", " ").strip().title()


# ============================================================================
# HTTP + OVERPASS + NOMINATIM
# ============================================================================
def http_json(url: str, *, data: Optional[bytes] = None, timeout: int = 180):
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def overpass_request(query: str):
    """Retry across multiple Overpass mirrors with exponential backoff."""
    payload = urllib.parse.urlencode({"data": query}).encode()
    last_error = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(MAX_RETRIES):
            try:
                return http_json(endpoint, data=payload, timeout=300)
            except Exception as exc:
                last_error = exc
                sleep_for = min(30, (2 ** attempt) + random.random())
                print(f"⚠️  Overpass failed "
                      f"(mirror={endpoint}, attempt={attempt + 1}/{MAX_RETRIES})")
                print(f"    {exc}")
                print(f"    retrying in {sleep_for:.1f}s")
                time.sleep(sleep_for)
    raise RuntimeError(f"All Overpass mirrors failed. Last error: {last_error}")


def lookup_bbox(place: str) -> tuple[float, float, float, float]:
    """Return (west, south, east, north)."""
    params = {"q": place, "format": "jsonv2", "limit": 1}
    url = f"{NOMINATIM}?{urllib.parse.urlencode(params)}"
    data = http_json(url)
    if not data:
        raise ValueError(f"Place not found: {place}")
    south, north, west, east = (float(x) for x in data[0]["boundingbox"])
    return (west, south, east, north)


def build_layer_query(
    bbox: tuple[float, float, float, float],
    selectors: list[tuple[str, Optional[str]]],
) -> str:
    """One query unioning every selector in a layer across node/way/relation."""
    west, south, east, north = bbox
    area = f"{south},{west},{north},{east}"
    parts = []
    for key, value in selectors:
        sel = f'["{key}"]' if value is None else f'["{key}"="{value}"]'
        parts.append(f"  node{sel}({area});")
        parts.append(f"  way{sel}({area});")
        parts.append(f"  relation{sel}({area});")
    body = "\n".join(parts)
    return f"[out:json][timeout:180];\n(\n{body}\n);\nout center tags qt;".strip()


def _coords(el: dict) -> tuple[Optional[float], Optional[float]]:
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


# ============================================================================
# CATEGORIES
# ============================================================================
def _primary_category(tags: dict) -> tuple[Optional[str], Optional[str]]:
    """(key, value) of the first present PRIMARY_CATEGORY_KEYS tag."""
    for key in PRIMARY_CATEGORY_KEYS:
        value = tags.get(key)
        if value:
            return key, value
    return None, None


def _category_values(tags: dict) -> list[str]:
    """All category-ish raw values for an element (primary + secondary), each a
    distinct token (cuisine/sport can be multi-valued: "pizza;burgers")."""
    values: list[str] = []
    _, primary = _primary_category(tags)
    if primary:
        values.append(primary)
    for key in SECONDARY_CATEGORY_KEYS:
        raw = tags.get(key)
        if raw:
            values.extend(p.strip() for p in raw.replace(",", ";").split(";") if p.strip())
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ============================================================================
# IMAGERY (free, no-key, ToS-clean — Wikimedia Commons via OSM's own tags)
# ============================================================================
COMMONS_THUMB_W = 800
WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def _commons_thumb(filename: Optional[str], width: int = COMMONS_THUMB_W) -> Optional[str]:
    """A Commons file name → a free, no-key thumbnail URL via Special:FilePath."""
    if not filename:
        return None
    name = filename.strip()
    if name.lower().startswith("category:"):
        return None
    name = re.sub(r"^file:", "", name, flags=re.IGNORECASE).strip().replace(" ", "_")
    if not name:
        return None
    return ("https://commons.wikimedia.org/wiki/Special:FilePath/"
            f"{urllib.parse.quote(name)}?width={width}")


def _image_from_tags(tags: dict) -> Optional[str]:
    """Zero-network image URL straight from an element's own tags."""
    url = _commons_thumb(tags.get("wikimedia_commons"))
    if url:
        return url
    img = tags.get("image")
    if not img:
        return None
    return img if img.startswith("http") else _commons_thumb(img)


def _wbget_p18(chunk: list, out: dict):
    """Fetch P18 image filenames for a chunk of QIDs into `out` (qid → thumbUrl)."""
    if not chunk:
        return
    params = {"action": "wbgetentities", "ids": "|".join(chunk),
              "props": "claims", "format": "json"}
    url = f"{WIKIDATA_API}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:                            # network hiccup: skip, keep going
        print(f"   ⚠ wikidata image fetch failed: {e}")
        return
    time.sleep(REQUEST_PAUSE_SEC)

    if data.get("error"):                             # a bad id poisoned the batch
        if len(chunk) == 1:
            return
        mid = len(chunk) // 2
        _wbget_p18(chunk[:mid], out)
        _wbget_p18(chunk[mid:], out)
        return

    for qid, ent in (data.get("entities") or {}).items():
        claims = (ent.get("claims") or {}).get("P18") or []
        try:
            fname = claims[0]["mainsnak"]["datavalue"]["value"]
        except (KeyError, IndexError, TypeError):
            continue
        thumb = _commons_thumb(fname)
        if thumb:
            out[qid] = thumb


def resolve_wikidata_images(qids) -> dict:
    """Batch-resolve Wikidata QIDs → Commons thumbnail URLs via the P18 (image)
    claim. Free, no key, ≤50 ids per call. Returns {qid: thumbUrl}."""
    out: dict = {}
    qids = [q for q in dict.fromkeys(qids) if re.fullmatch(r"Q\d+", q or "")]
    if not qids:
        return out
    print(f"🖼  resolving {len(qids):,} wikidata images…")
    for i in range(0, len(qids), 50):
        _wbget_p18(qids[i:i + 50], out)
    print(f"   ✅ {len(out):,} images resolved")
    return out


# ============================================================================
# NODE SHAPING
# ============================================================================
def _locality(tags: dict, default_city: Optional[str], default_region: Optional[str]):
    """(slug, title, region) from addr:* tags, falling back to the queried place."""
    city = tags.get("addr:city") or default_city
    region = _norm_region(tags.get("addr:state") or default_region)
    if not (city or region):
        return None, None, None
    title = ", ".join(p for p in (city, region) if p)
    return _slug(city or "", region or ""), title, region


def shape_node(osm_id: str, node_type: str, tags: dict, lat, lon,
               locality_title: Optional[str],
               image_url: Optional[str] = None) -> dict:
    """Shape one OSM element into a Fangorn node `fields` dict for `node_type`."""
    name = tags.get("name")
    _, primary = _primary_category(tags)
    label = _humanize(primary) if primary else node_type.lower()
    coords = f"{lat},{lon}" if lat is not None and lon is not None else None
    where = f" in {locality_title}" if locality_title else ""

    desc = tags.get("description")
    website = tags.get("website") or tags.get("contact:website")
    phone = tags.get("phone") or tags.get("contact:phone")

    if node_type == "Trail":
        route = tags.get("route") or tags.get("piste:type") or "trail"
        distance = tags.get("distance") or tags.get("length")
        text = (f"{name} — {_humanize(route)} trail{where}"
                + (f", {distance}" if distance else "")
                + (f". {desc}" if desc else ""))
        extra = {"routeType": route, "distance": distance,
                 "surface": tags.get("surface"), "network": tags.get("network")}
    elif node_type == "Lake":
        text = f"{name} — lake{where}" + (f". {desc}" if desc else "")
        extra = {"waterType": tags.get("water") or tags.get("natural")}
    elif node_type == "Landmark":
        text = (f"{name} — {label}{where}" + (f". {desc}" if desc else ""))
        extra = {"landmarkType": primary}
    else:  # Business
        cuisine = tags.get("cuisine")
        text = (f"{name} — {label}{where}"
                + (f". Cuisine: {cuisine.replace(';', ', ')}" if cuisine else "")
                + (f". {desc}" if desc else ""))
        extra = {"cuisine": cuisine,
                 "openingHours": tags.get("opening_hours"),
                 "address": " ".join(p for p in (
                     tags.get("addr:housenumber"), tags.get("addr:street")) if p) or None}

    fields = {
        "schemaVersion": SCHEMA_VERSION,
        "entityType": node_type,
        "osmId": osm_id,
        "title": name,
        "primaryType": label,
        "coordinates": coords,
        "locality": locality_title,
        "website": website,
        "phone": phone,
        "imageUrl": image_url,
        "source": "osm",
        "text": text,
    }
    fields.update(extra)
    return _clean(fields)


def shape_category(raw: str) -> dict:
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Category",
        "categoryId": _slug(raw), "title": _humanize(raw), "rawType": raw,
        "text": f"{_humanize(raw)} — category",
    })


def shape_locality(slug: str, title: str, region: Optional[str]) -> dict:
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Locality",
        "localityId": slug, "title": title, "region": region,
        "text": f"{title} — locality",
    })


# ============================================================================
# THE SOURCE
# ============================================================================
class OsmSource(SourceBase):
    name = "osm"
    default_volume = 1
    edges_stem = "osm_edges"
    stems = {"Business": "osm_businesses", "Trail": "osm_trails",
             "Lake": "osm_lakes", "Landmark": "osm_landmarks",
             "Category": "osm_categories", "Locality": "osm_localities"}
    snapshot_stems = set(stems.values())   # batch source — replace wholesale each run
    role_map = {"title": "title", "subtitle": "locality",
                "tags": ["primaryType", "source"], "text": ["text"]}
    presentation = {"accent": "#7ebc6f", "icons": {
        "Business": "storefront", "Trail": "route", "Lake": "water",
        "Landmark": "attractions", "Category": "sell", "Locality": "place"}}

    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--place", default="",
                       help='Named place to ingest, e.g. "Eagle River, Wisconsin". '
                            'Geocoded via Nominatim unless --bbox is given; either way it '
                            'supplies the default locality label.')
        p.add_argument("--bbox", default="",
                       help='Explicit bounding box "W,S,E,N" (lng,lat) overriding Nominatim.')
        p.add_argument("--near-radius-m", type=float, default=0.0,
                       help="Emit `near` edges between OSM nodes within this many metres "
                            "(0 = off; O(n²), so use a small radius on dense areas).")
        p.add_argument("--no-images", action="store_true",
                       help="Skip Wikimedia/Wikidata image resolution (faster, no imageUrl).")

    def read(self, cursor: int, args: argparse.Namespace) -> list[dict]:
        if not args.place and not args.bbox:
            raise SystemExit("Provide --place (geocoded) and/or --bbox (explicit W,S,E,N).")
        # Locality fallback for elements lacking addr:* tags: parse "<city>, <region>".
        parts = [p.strip() for p in args.place.split(",")] if args.place else []
        self._default_city = parts[0] if parts else None
        self._default_region = parts[1] if len(parts) > 1 else None
        self._near_radius = args.near_radius_m

        if args.bbox:
            try:
                bbox = tuple(float(x) for x in args.bbox.split(","))
                if len(bbox) != 4:
                    raise ValueError
            except ValueError:
                raise SystemExit('--bbox must be "W,S,E,N" (four comma-separated numbers).')
            print(f"📦 bbox (W,S,E,N) = {bbox} [explicit]")
        else:
            print(f"🌍 Geocoding: {args.place}")
            bbox = lookup_bbox(args.place)
            print(f"📦 bbox (W,S,E,N) = {bbox}")
        time.sleep(REQUEST_PAUSE_SEC)

        with_images = not args.no_images
        seen_osm: set[str] = set()
        records: list[dict] = []
        # Fetch + assign node_type + resolve images + cross-layer dedup (first,
        # most-specific layer claims an osm_id). All network IO lives here; the layer
        # ORDER matters for dedup, so it is a read-side concern.
        for label, node_type, selectors in LAYERS:
            print(f"🔎 {label} → {node_type}")
            payload = overpass_request(build_layer_query(bbox, selectors))
            qid_images: dict = {}
            if with_images:
                need_qids = [
                    t.get("wikidata") for el in payload.get("elements", [])
                    if (t := el.get("tags") or {}).get("name")
                    and not _image_from_tags(t) and t.get("wikidata")
                ]
                qid_images = resolve_wikidata_images(need_qids)
            added = 0
            for el in payload.get("elements", []):
                tags = el.get("tags") or {}
                if not tags.get("name"):
                    continue
                osm_id = f"{el.get('type')}/{el.get('id')}"
                if osm_id in seen_osm:
                    continue
                seen_osm.add(osm_id)
                lat, lon = _coords(el)
                image_url = (_image_from_tags(tags) or qid_images.get(tags.get("wikidata"))) \
                    if with_images else None
                records.append({"osm_id": osm_id, "node_type": node_type, "tags": tags,
                                "lat": lat, "lon": lon, "image_url": image_url})
                added += 1
            print(f"   +{added:,} {node_type.lower()} nodes")
            time.sleep(REQUEST_PAUSE_SEC)
        return records

    def build_graph(self, records: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
        """Elements → typed nodes + shared Category/Locality hubs + edges. Pure w.r.t.
        `records`; closes over default city/region + near radius stashed by `read`.
        Mirrors the old `osm.run_export` write order exactly."""
        default_city = getattr(self, "_default_city", None)
        default_region = getattr(self, "_default_region", None)
        near_radius_m = getattr(self, "_near_radius", 0.0)

        buckets: dict[str, list[dict]] = {"Business": [], "Trail": [],
                                          "Lake": [], "Landmark": []}
        categories: list[dict] = []
        localities: list[dict] = []
        edges: list[dict] = []
        seen_cat: set[str] = set()
        seen_loc: set[str] = set()
        coords_by_id: list[tuple[str, str, tuple[float, float]]] = []

        def edge(rel, frm, to, ft, tt, **extra):
            edges.append(_clean({"rel": rel, "from": frm, "to": to,
                                 "fromType": ft, "toType": tt, **extra}))

        for r in records:
            tags, osm_id, node_type = r["tags"], r["osm_id"], r["node_type"]
            lat, lon = r["lat"], r["lon"]
            lslug, ltitle, region = _locality(tags, default_city, default_region)
            if lslug and lslug not in seen_loc:
                seen_loc.add(lslug)
                localities.append({"name": lslug,
                                   "fields": shape_locality(lslug, ltitle, region)})
            buckets[node_type].append({
                "name": osm_id,
                "fields": shape_node(osm_id, node_type, tags, lat, lon, ltitle, r["image_url"]),
            })
            if lat is not None and lon is not None:
                coords_by_id.append((osm_id, node_type, (lat, lon)))
            if lslug:
                edge("locatedIn", osm_id, lslug, node_type, "Locality")
            for raw in _category_values(tags):
                cslug = _slug(raw)
                if cslug not in seen_cat:
                    seen_cat.add(cslug)
                    categories.append({"name": cslug, "fields": shape_category(raw)})
                edge("inCategory", osm_id, cslug, node_type, "Category")

        # near edges (any OSM node ↔ any OSM node within radius; both directions)
        if near_radius_m > 0:
            for i in range(len(coords_by_id)):
                for j in range(i + 1, len(coords_by_id)):
                    d = _haversine_m(coords_by_id[i][2], coords_by_id[j][2])
                    if d <= near_radius_m:
                        ai, at, _ = coords_by_id[i]
                        bi, bt, _ = coords_by_id[j]
                        edge("near", ai, bi, at, bt, meters=round(d))
                        edge("near", bi, ai, bt, at, meters=round(d))

        nodes = {"Business": buckets["Business"], "Trail": buckets["Trail"],
                 "Lake": buckets["Lake"], "Landmark": buckets["Landmark"],
                 "Category": categories, "Locality": localities}
        return nodes, edges

    def next_cursor(self, records: list[dict], prev: int) -> int:
        return prev  # batch source — no live tail to advance past


def run() -> None:
    run_source(OsmSource())


if __name__ == "__main__":
    run()
