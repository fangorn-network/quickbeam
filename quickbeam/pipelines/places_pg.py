"""
places_pg.py — Postgres `places_raw` (Google Places payloads) → Fangorn graph.

The local-business analog of mb_pg.py. Where mb_pg walks the MusicBrainz
relational schema, this walks the raw Place Details payloads scraped by
places.py and emits the same node/edge volume files the rest of the pipeline
consumes (schemagen → build → cdn). Adding a node type = a shaper + a couple of
lines, same spirit as mb_pg's ENTITIES registry.

  Nodes : Business, Review, Category, Reviewer, Locality
  Edges : hasReview  (Business→Review),  byAuthor  (Review→Reviewer),
          inCategory (Business→Category), locatedIn (Business→Locality),
          near       (Business→Business, within --near-radius-m)

Output (matches mb_pg.py so schemagen/build are unchanged):
  volume_<n>_businesses.json / _reviews.json / _categories.json /
  _reviewers.json / _localities.json  +  volume_<n>_edges.json

Node `name` is a stable id: Business = place_id, Review = "<place_id>:<i>",
Category/Locality = slug, Reviewer = stable author key. The downstream demo can
anchor on the one Business whose payload was marked is_anchor (carried through as
the `isAnchor` field).

The node/edge model is intentionally source-agnostic: a future source (OSM,
Wikidata — see OSM_ENHANCE_INVESTIGATE.md) can emit the same Business/Category/
Locality types into this schema.

Requires: psycopg[binary].
"""
import os
import json
import math
import argparse
import hashlib
import re

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

SCHEMA_VERSION = 1
DEFAULT_DSN = os.environ.get(
    "PLACES_PG_DSN", "postgresql://places:places@localhost:5432/places_db"
)

PRICE_LABELS = {
    "PRICE_LEVEL_FREE": "free",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}
# servesBeer → "serves beer", liveMusic → "live music", etc.
AMENITY_FLAGS = {
    "servesBeer": "serves beer", "servesWine": "serves wine",
    "servesCocktails": "serves cocktails", "liveMusic": "live music",
    "outdoorSeating": "outdoor seating", "goodForChildren": "good for children",
    "reservable": "reservable", "delivery": "delivery", "dineIn": "dine-in",
    "takeout": "takeout",
}


# ===========================================================================
# OUTPUT (same JSON-array streaming writer as mb_pg.py)
# ===========================================================================
class JsonArrayWriter:
    def __init__(self, path: str):
        self._f = open(path, "w", encoding="utf-8")
        self._f.write("[\n")
        self._first = True
        self.count = 0

    def write(self, obj: dict):
        sep = "" if self._first else ",\n"
        self._f.write(f"{sep}  {json.dumps(obj, ensure_ascii=False, default=str)}")
        self._first = False
        self.count += 1

    def close(self):
        self._f.write("\n]")
        self._f.close()


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items()
            if v is not None and v != "" and v != [] and v != {}}


def _slug(*parts: str) -> str:
    s = "-".join(p for p in parts if p)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "unknown"


def _author_key(attr: dict) -> str:
    """Stable reviewer id: prefer the Google contributor URI, else hash the name."""
    uri = (attr or {}).get("uri") or ""
    if uri:
        return "rev-" + hashlib.sha1(uri.encode()).hexdigest()[:16]
    name = (attr or {}).get("displayName") or "anonymous"
    return "rev-" + hashlib.sha1(name.encode()).hexdigest()[:16]


def _haversine_m(a: tuple, b: tuple) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ===========================================================================
# PAYLOAD → NODE/EDGE SHAPING
# ===========================================================================
def _locality(payload: dict) -> tuple[str | None, str | None, str | None]:
    """(slug, title, region) from addressComponents (locality + admin region)."""
    city = region = None
    for comp in payload.get("addressComponents", []) or []:
        types = comp.get("types", [])
        if "locality" in types and not city:
            city = comp.get("longText")
        elif "administrative_area_level_1" in types and not region:
            region = comp.get("shortText") or comp.get("longText")
    if not (city or region):
        return None, None, None
    title = ", ".join(p for p in (city, region) if p)
    return _slug(city or "", region or ""), title, region


def _amenities(payload: dict) -> list[str]:
    return [label for key, label in AMENITY_FLAGS.items() if payload.get(key) is True]


def _hours(payload: dict) -> str | None:
    hrs = (payload.get("regularOpeningHours") or {}).get("weekdayDescriptions")
    return "; ".join(hrs) if hrs else None


def shape_business(place_id: str, payload: dict, is_anchor: bool) -> dict:
    name = (payload.get("displayName") or {}).get("text") or "(unnamed)"
    loc = payload.get("location") or {}
    coords = (f"{loc['latitude']},{loc['longitude']}"
              if loc.get("latitude") is not None else None)
    price = PRICE_LABELS.get(payload.get("priceLevel"))
    primary = (payload.get("primaryTypeDisplayName") or {}).get("text") or payload.get("primaryType")
    editorial = (payload.get("editorialSummary") or {}).get("text")
    amenities = _amenities(payload)
    rating, n = payload.get("rating"), payload.get("userRatingCount")
    _, locality_title, _ = _locality(payload)

    # The embedded blurb leads with the high-value descriptive signal (name, type,
    # locality, editorial, amenities). Hours are deliberately excluded: a full
    # weekly schedule string carries no semantic-search value and only crowds out
    # the descriptive/review content within the embedding's token budget. Hours
    # remain available as a structured field below (display + the open-now filter).
    text = (f"{name} — {primary or 'local business'}"
            + (f" in {locality_title}" if locality_title else "")
            + (f". {editorial.rstrip('.')}" if editorial else "")
            + (f". Rated {rating}/5 from {n:,} reviews" if rating and n else "")
            + (f". Price {price}" if price else "")
            + (f". {', '.join(amenities)}" if amenities else ""))

    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Business",
        "placeId": place_id, "title": name,
        "primaryType": primary, "categories": payload.get("types"),
        "address": payload.get("formattedAddress"),
        "shortAddress": payload.get("shortFormattedAddress"),
        "coordinates": coords, "locality": locality_title,
        "phone": payload.get("nationalPhoneNumber"),
        "website": payload.get("websiteUri"),
        "googleMapsUri": payload.get("googleMapsUri"),
        "rating": rating, "userRatingCount": n, "priceLevel": price,
        "businessStatus": payload.get("businessStatus"),
        "hours": _hours(payload),
        "editorialSummary": editorial,
        "amenities": amenities,
        "isAnchor": is_anchor or None,
        "text": text,
    })


def shape_review(place_id: str, biz_name: str, idx: int, review: dict) -> tuple[dict, dict, str]:
    """Return (review_fields, reviewer_fields, author_key)."""
    attr = review.get("authorAttribution") or {}
    author = attr.get("displayName") or "anonymous"
    body = (review.get("text") or {}).get("text") or (review.get("originalText") or {}).get("text") or ""
    rating = review.get("rating")
    when = review.get("relativePublishTimeDescription")
    rid = f"{place_id}:{idx}"
    text = (f"Review of {biz_name}"
            + (f" ({rating}/5)" if rating else "")
            + (f" by {author}" if author else "")
            + (f", {when}" if when else "")
            + (f": {body}" if body else ""))
    node = _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Review",
        "reviewId": rid, "title": f"{author} on {biz_name}",
        "businessId": place_id, "author": author,
        "rating": rating, "body": body[:4000] or None,
        "relativeTime": when, "publishTime": review.get("publishTime"),
        "text": text,
    })
    akey = _author_key(attr)
    reviewer = _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Reviewer",
        "reviewerId": akey, "title": author, "profileUri": attr.get("uri"),
        "text": f"{author} — Google reviewer",
    })
    return node, reviewer, akey


def shape_category(type_str: str, display: str | None) -> dict:
    title = display or type_str.replace("_", " ").title()
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Category",
        "categoryId": _slug(type_str), "title": title, "rawType": type_str,
        "text": f"{title} — business category",
    })


def shape_locality(slug: str, title: str, region: str | None) -> dict:
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Locality",
        "localityId": slug, "title": title, "region": region,
        "text": f"{title} — locality",
    })


# ===========================================================================
# RAW SOURCE — Postgres or a JSONL file. Each yields dicts:
# {"place_id", "is_anchor", "payload"}.
# ===========================================================================
def iter_db_rows(conn):
    with conn.cursor(name="places_stream", row_factory=psycopg.rows.dict_row) as cur:
        cur.itersize = 1000
        cur.execute("SELECT place_id, is_anchor, payload FROM places_raw")
        yield from cur


def iter_jsonl_rows(path: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            payload = rec.get("payload", rec)  # bare payloads also accepted
            yield {
                "place_id": rec.get("place_id") or payload.get("id"),
                "is_anchor": bool(rec.get("is_anchor", False)),
                "payload": payload,
            }


# ===========================================================================
# EXTRACTION
# ===========================================================================
def run_export(rows, out_dir: str, volume: int, near_radius_m: float):
    paths = {
        t: os.path.join(out_dir, f"volume_{volume}_{stem}.json")
        for t, stem in [("Business", "businesses"), ("Review", "reviews"),
                        ("Category", "categories"), ("Reviewer", "reviewers"),
                        ("Locality", "localities")]
    }
    writers = {t: JsonArrayWriter(p) for t, p in paths.items()}
    edges = JsonArrayWriter(os.path.join(out_dir, f"volume_{volume}_edges.json"))

    seen_cat: set[str] = set()
    seen_loc: set[str] = set()
    seen_rev: set[str] = set()
    biz_coords: list[tuple[str, tuple[float, float]]] = []

    def edge(rel, frm, to, ft, tt, **extra):
        edges.write(_clean({"rel": rel, "from": frm, "to": to,
                            "fromType": ft, "toType": tt, **extra}))

    for row in rows:
        pid, payload = row["place_id"], row["payload"]
        name = (payload.get("displayName") or {}).get("text") or pid

        # --- Business node ---
        writers["Business"].write({"name": pid, "fields": shape_business(pid, payload, row["is_anchor"])})
        loc = payload.get("location") or {}
        if loc.get("latitude") is not None:
            biz_coords.append((pid, (loc["latitude"], loc["longitude"])))

        # --- Categories ---
        primary_type = payload.get("primaryType")
        primary_disp = (payload.get("primaryTypeDisplayName") or {}).get("text")
        for t in payload.get("types", []) or []:
            cslug = _slug(t)
            if cslug not in seen_cat:
                seen_cat.add(cslug)
                disp = primary_disp if t == primary_type else None
                writers["Category"].write({"name": cslug, "fields": shape_category(t, disp)})
            edge("inCategory", pid, cslug, "Business", "Category")

        # --- Locality ---
        lslug, ltitle, region = _locality(payload)
        if lslug:
            if lslug not in seen_loc:
                seen_loc.add(lslug)
                writers["Locality"].write({"name": lslug, "fields": shape_locality(lslug, ltitle, region)})
            edge("locatedIn", pid, lslug, "Business", "Locality")

        # --- Reviews + Reviewers ---
        for i, rv in enumerate(payload.get("reviews", []) or []):
            rnode, reviewer, akey = shape_review(pid, name, i, rv)
            rid = rnode["reviewId"]
            writers["Review"].write({"name": rid, "fields": rnode})
            edge("hasReview", pid, rid, "Business", "Review")
            if akey not in seen_rev:
                seen_rev.add(akey)
                writers["Reviewer"].write({"name": akey, "fields": reviewer})
            edge("byAuthor", rid, akey, "Review", "Reviewer")

    # --- near edges (Business↔Business within radius; both directions) ---
    if near_radius_m > 0:
        n_near = 0
        for i in range(len(biz_coords)):
            for j in range(i + 1, len(biz_coords)):
                d = _haversine_m(biz_coords[i][1], biz_coords[j][1])
                if d <= near_radius_m:
                    a, b = biz_coords[i][0], biz_coords[j][0]
                    edge("near", a, b, "Business", "Business", meters=round(d))
                    edge("near", b, a, "Business", "Business", meters=round(d))
                    n_near += 1
        print(f"   🧭 near: {n_near} business pairs within {near_radius_m:.0f}m")

    for t, w in writers.items():
        w.close()
        print(f"   ✅ {t:<9}: {w.count:,} → {os.path.basename(paths[t])}")
    edges.close()
    print(f"   ✅ edges    : {edges.count:,}")


# ===========================================================================
# CLI
# ===========================================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Convert Postgres places_raw (Google Places) into a Fangorn graph.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres connection string (or env PLACES_PG_DSN).")
    p.add_argument("--raw-in", default="",
                   help="Read raw payloads from this JSONL file instead of Postgres "
                        "(the no-database path; see places.py --raw-out).")
    p.add_argument("--output-dir", default="./stage_volumes")
    p.add_argument("--volume", type=int, default=1)
    p.add_argument("--near-radius-m", type=float, default=1500.0,
                   help="Emit `near` edges between businesses within this many metres (0 = off).")
    return p.parse_args()


def run():
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    if args.raw_in:
        print(f"📄 Source: {args.raw_in}")
        run_export(iter_jsonl_rows(args.raw_in), args.output_dir, args.volume, args.near_radius_m)
    else:
        if psycopg is None:
            raise SystemExit("psycopg not installed. Run: pip install 'psycopg[binary]' "
                             "(or use --raw-in to skip Postgres).")
        print(f"🔌 Connecting: {args.dsn.rsplit('@', 1)[-1]}")
        with psycopg.connect(args.dsn) as conn:
            run_export(iter_db_rows(conn), args.output_dir, args.volume, args.near_radius_m)
    print("\n📊 Done. Next: quickbeam data schemagen --prefix fangorn.places "
          "--bundle-name localcore --version v1")


if __name__ == "__main__":
    run()
