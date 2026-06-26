"""
places.py — Google Places API (new, v1) scraper → Postgres `places_raw`.

The local-SMB analog of the MusicBrainz mbdump → Postgres step. Where mbdump
ships a giant relational dump, here we *build* the raw store ourselves: sweep an
area (e.g. "bars near Eagle River, WI"), pull full Place Details (hours, contact,
rating, categories, and the contextual user reviews) for every hit, and stash the
verbatim API payload as jsonb. `places_pg.py` then turns that store into the
Fangorn node/edge graph — exactly as `mb_pg.py` walks the MusicBrainz Postgres.

Storing the raw payload in Postgres means reprocessing (schema tweaks, new node
types) never re-hits the paid API. One business in the sweep can be marked the
`--anchor` (the pitch target, e.g. Shotski's) so the downstream demo can centre
on it.

Note on Google's terms: the Places API restricts long-term caching of Places
content (notably review text). Treat this store as a short-lived prototype cache,
not a permanent warehouse. OpenStreetMap (see osm.py) is the ToS-clean source for
POIs/hours/categories and can feed the same downstream schema (minus reviews).

Requires: requests, psycopg[binary]  (both already in pyproject).
Auth: a Google Cloud key with "Places API (new)" + billing, via --api-key or
env GOOGLE_PLACES_API_KEY.
"""
import os
import sys
import json
import time
import math
import argparse

import requests

try:
    import psycopg
except ImportError:  # pragma: no cover - surfaced at runtime with a clear hint
    psycopg = None

DEFAULT_DSN = os.environ.get(
    "PLACES_PG_DSN", "postgresql://places:places@localhost:5432/places_db"
)

SEARCH_TEXT_URL   = "https://places.googleapis.com/v1/places:searchText"
SEARCH_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
DETAILS_URL       = "https://places.googleapis.com/v1/places/{place_id}"

# Field masks (Places API "new" uses camelCase dotted paths). Search returns only
# what we need to fan out to Details; Details pulls the full business record.
SEARCH_FIELD_MASK = "places.id,nextPageToken"
DETAILS_FIELD_MASK = ",".join([
    "id", "displayName", "formattedAddress", "shortFormattedAddress",
    "addressComponents", "location", "rating", "userRatingCount", "priceLevel",
    "types", "primaryType", "primaryTypeDisplayName", "businessStatus",
    "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri",
    "googleMapsUri", "regularOpeningHours", "currentOpeningHours",
    "editorialSummary", "reviews", "goodForChildren", "servesBeer",
    "servesWine", "servesCocktails", "liveMusic", "outdoorSeating",
    "reservable", "delivery", "dineIn", "takeout",
])

REQUEST_PAUSE_SEC = 0.4  # be polite; Places API also rate-limits server-side

# Billable-call tally for this run. Search uses an IDs-only field mask (cheap
# Essentials tier); Details uses the full mask → Enterprise+Atmosphere tier
# (reviews are the priciest field group). Reported at the end so you can watch
# usage against Google's monthly free allotment per SKU.
CALLS = {"search": 0, "details": 0}


# ===========================================================================
# POSTGRES
# ===========================================================================
DDL = """
CREATE TABLE IF NOT EXISTS places_raw (
    place_id    text PRIMARY KEY,
    query       text,
    is_anchor   boolean DEFAULT false,
    fetched_at  timestamptz DEFAULT now(),
    payload     jsonb NOT NULL
);
"""


def _connect(dsn: str):
    if psycopg is None:
        raise SystemExit("psycopg not installed. Run: pip install 'psycopg[binary]'")
    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    return conn


def _upsert(conn, place_id: str, query: str, is_anchor: bool, payload: dict):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO places_raw (place_id, query, is_anchor, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (place_id) DO UPDATE SET
                payload    = EXCLUDED.payload,
                fetched_at = now(),
                -- once an anchor, always an anchor (don't let a later sweep clear it)
                is_anchor  = places_raw.is_anchor OR EXCLUDED.is_anchor,
                query      = COALESCE(places_raw.query, EXCLUDED.query)
            """,
            (place_id, query, is_anchor, json.dumps(payload)),
        )
    conn.commit()


# ===========================================================================
# GOOGLE PLACES API (new)
# ===========================================================================
def _headers(api_key: str, field_mask: str) -> dict:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
    }


def search_text(api_key: str, query: str, max_results: int) -> list[str]:
    """Text Search → up to `max_results` place IDs (paged, 20/page, 60 max)."""
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        body = {"textQuery": query}
        if page_token:
            body["pageToken"] = page_token
        CALLS["search"] += 1
        resp = requests.post(SEARCH_TEXT_URL, headers=_headers(api_key, SEARCH_FIELD_MASK),
                             json=body, timeout=30)
        if resp.status_code != 200:
            print(f"   ⚠️  searchText {resp.status_code}: {resp.text[:300]}")
            break
        data = resp.json()
        for p in data.get("places", []):
            if p.get("id"):
                ids.append(p["id"])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(REQUEST_PAUSE_SEC)
    return ids[:max_results]

def search_nearby(api_key: str, lat: float, lng: float, radius_m: float,
                  included_types: list[str], max_results: int) -> list[str]:
    """Nearby Search → up to `max_results` place IDs within radius_m of lat/lng."""
    body = {
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_m}
        },
        "maxResultCount": min(max_results, 20),
    }
    if included_types:
        body["includedTypes"] = included_types
    CALLS["search"] += 1
    
    # 🟢 FIX: Use "places.id" directly instead of SEARCH_FIELD_MASK
    resp = requests.post(SEARCH_NEARBY_URL, headers=_headers(api_key, "places.id"),
                         json=body, timeout=30)
    
    if resp.status_code != 200:
        print(f"   ⚠️  searchNearby {resp.status_code}: {resp.text[:300]}")
        return []
    return [p["id"] for p in resp.json().get("places", []) if p.get("id")][:max_results]

# The new Nearby Search has no pagination (no nextPageToken) and a hard 20-result
# cap per call, ranked by Google's opaque "prominence". A single call over a dense
# area therefore silently drops everything past the top 20. The only way to sweep
# an area to completion is to recursively subdivide any circle that comes back
# *full* (== the cap) until each sub-circle returns fewer than the cap — at which
# point you know you've captured everything inside it. See `sweep_area`.
NEARBY_CAP = 20


def _offset_latlng(lat: float, lng: float, d_north_m: float, d_east_m: float):
    """Shift (lat,lng) by d_north_m / d_east_m metres (local flat-earth approx)."""
    d_lat = d_north_m / 111_320.0
    d_lng = d_east_m / (111_320.0 * math.cos(math.radians(lat)) or 1e-9)
    return lat + d_lat, lng + d_lng


def sweep_area(api_key: str, lat: float, lng: float, radius_m: float,
               included_types: list[str], min_radius_m: float,
               max_tiles: int, _seen: set[str] | None = None,
               _depth: int = 0) -> set[str]:
    """Recursive grid subdivision (quadtree tiling) to beat the 20-result cap.

    Search the circle at (lat,lng,radius_m). If it comes back *full* (NEARBY_CAP
    hits) the area almost certainly holds more than we can see, so split it into
    four overlapping sub-circles (NW/NE/SW/SE) at ~0.6× the radius and recurse.
    Stop subdividing once a circle returns fewer than the cap (we got them all)
    or the radius would drop below `min_radius_m` (avoid infinite zoom on a single
    hyper-dense block). `max_tiles` is a hard ceiling on total search calls.

    Returns the deduped set of place IDs found across every tile.
    """
    seen = _seen if _seen is not None else set()
    if CALLS["search"] >= max_tiles:
        return seen

    ids = search_nearby(api_key, lat, lng, radius_m, included_types, NEARBY_CAP)
    new = [i for i in ids if i not in seen]
    seen.update(ids)
    indent = "  " * _depth
    full = len(ids) >= NEARBY_CAP
    print(f"   {indent}◻ r={radius_m:>6.0f}m @ {lat:.4f},{lng:.4f} → "
          f"{len(ids):>2} hits (+{len(new)} new){'  ⚠️ FULL, subdividing' if full else ''}")

    # Below the cap → captured everything in this circle, no need to dig deeper.
    if not full:
        return seen

    # Quarters are placed diagonally (NW/NE/SW/SE), so the parent's *cardinal*
    # edges (the N/S/E/W extremes) are the hardest points to cover. A cardinal
    # extreme sits 0.707R from the nearest quarter centre (offset 0.5R per axis),
    # so the sub-radius must be ≥ 0.707R to fully tile the parent — anything less
    # leaves an uncovered gap ring and silently drops the venues inside it. Use
    # 0.75 for a small margin over the 0.707 floor (and the flat-earth approx).
    sub_radius = radius_m * 0.75
    if sub_radius < min_radius_m:
        print(f"   {indent}  ⛔ hit min radius {min_radius_m:.0f}m — some venues here "
              f"may still be hidden by Google's cap")
        return seen

    off = radius_m * 0.5  # centre offset of each quarter from the parent centre
    for d_n, d_e in ((off, -off), (off, off), (-off, -off), (-off, off)):  # NW NE SW SE
        if CALLS["search"] >= max_tiles:
            print(f"   {indent}  ⛔ hit --max-tiles {max_tiles} — sweep truncated")
            break
        slat, slng = _offset_latlng(lat, lng, d_n, d_e)
        time.sleep(REQUEST_PAUSE_SEC)
        sweep_area(api_key, slat, slng, sub_radius, included_types,
                   min_radius_m, max_tiles, _seen=seen, _depth=_depth + 1)
    return seen


def place_details(api_key: str, place_id: str) -> dict | None:
    CALLS["details"] += 1
    resp = requests.get(DETAILS_URL.format(place_id=place_id),
                        headers=_headers(api_key, DETAILS_FIELD_MASK), timeout=30)
    if resp.status_code != 200:
        print(f"   ⚠️  details {place_id} {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


def _is_anchor(payload: dict, anchor: str | None) -> bool:
    if not anchor:
        return False
    name = (payload.get("displayName") or {}).get("text", "") or ""
    return anchor.lower() in name.lower()


# ===========================================================================
# CLI
# ===========================================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Scrape Google Places (new API) for an area into Postgres places_raw.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--api-key", default=os.environ.get("GOOGLE_PLACES_API_KEY", ""),
                   help="Google Cloud key with Places API (new) (or env GOOGLE_PLACES_API_KEY).")
    p.add_argument("--dsn", default=DEFAULT_DSN,
                   help="Postgres connection string (or env PLACES_PG_DSN).")
    p.add_argument("--no-db", action="store_true", default=False,
                   help="Skip Postgres entirely (requires --raw-out). The no-database path.")
    p.add_argument("--raw-out", default="",
                   help="Also append each raw payload to this JSONL file "
                        "(feed it to `placespg --raw-in` without a database).")
    # one of: --query (Text Search) OR --location + --radius (Nearby Search)
    p.add_argument("--query", default="",
                   help='Text Search query, e.g. "bars near Eagle River, WI".')
    p.add_argument("--location", default="",
                   help="lat,lng for Nearby Search (used when --query is omitted).")
    p.add_argument("--radius", type=float, default=2000.0, help="Nearby Search radius (meters).")
    p.add_argument("--sweep", action="store_true", default=False,
                   help="Adaptive recursive grid subdivision (quadtree) for Nearby Search: "
                        "start at --location/--radius and auto-subdivide any circle that hits "
                        "Google's 20-result cap until the whole area is captured. Beats the cap.")
    p.add_argument("--min-radius", type=float, default=500.0,
                   help="Sweep floor: stop subdividing once sub-circles fall below this radius "
                        "(meters). Guards against infinite zoom on a hyper-dense block.")
    p.add_argument("--max-tiles", type=int, default=200,
                   help="Sweep guard: hard ceiling on total (cheap) Search calls per sweep.")
    p.add_argument("--types", default="bar,restaurant,night_club",
                   help="Comma-separated includedTypes for Nearby Search.")
    p.add_argument("--anchor", default="",
                   help='Mark businesses whose name contains this as the pitch anchor (e.g. "Shotskis").')
    p.add_argument("--max-results", type=int, default=60,
                   help="Max businesses to pull (Text Search caps at 60).")
    p.add_argument("--max-details", type=int, default=60,
                   help="Hard ceiling on billable Place Details calls per run (cost guard).")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Run only the (cheap) search and report how many billable Place "
                        "Details calls a real run would make — fetches no details, stores nothing.")
    p.add_argument("--details-only", default="",
                   help="Skip search; fetch details for these comma-separated place IDs only.")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.api_key:
        raise SystemExit("No API key. Pass --api-key or set GOOGLE_PLACES_API_KEY.")
    if not (args.query or args.location or args.details_only):
        raise SystemExit("Provide --query, or --location lat,lng, or --details-only IDs.")
    if args.no_db and not args.raw_out:
        raise SystemExit("--no-db requires --raw-out (nowhere to put the data otherwise).")

    conn = None
    if not args.no_db:
        conn = _connect(args.dsn)
        print(f"🔌 Postgres: {args.dsn.rsplit('@', 1)[-1]}")
    raw_out = open(args.raw_out, "a", encoding="utf-8") if args.raw_out else None
    if raw_out:
        print(f"📄 Raw JSONL → {args.raw_out}")

    # 1) Resolve the set of place IDs to fetch details for.
    if args.details_only:
        ids = [s.strip() for s in args.details_only.split(",") if s.strip()]
        label = "(details-only)"
    elif args.query:
        print(f"🔎 Text Search: {args.query!r} (≤{args.max_results})")
        ids = search_text(args.api_key, args.query, args.max_results)
        label = args.query
    else:
        lat, lng = (float(x) for x in args.location.split(","))
        types = [t.strip() for t in args.types.split(",") if t.strip()]
        if args.sweep:
            print(f"🔎 Adaptive sweep: {lat},{lng} r={args.radius}m types={types} "
                  f"(min-radius={args.min_radius}m, max-tiles={args.max_tiles})")
            ids = sorted(sweep_area(args.api_key, lat, lng, args.radius, types,
                                    args.min_radius, args.max_tiles))
            print(f"   sweep used {CALLS['search']} Search call(s)")
        else:
            print(f"🔎 Nearby Search: {lat},{lng} r={args.radius}m types={types}")
            ids = search_nearby(args.api_key, lat, lng, args.radius, types, args.max_results)
        label = f"nearby:{args.location}"
    print(f"   found {len(ids)} place id(s)")

    # Cost guard: never make more billable Details calls than --max-details.
    if len(ids) > args.max_details:
        print(f"   ✂️  capping {len(ids)} → {args.max_details} details (--max-details)")
        ids = ids[:args.max_details]

    if args.dry_run:
        if conn is not None:
            conn.close()
        if raw_out:
            raw_out.close()
        print(f"\n🔍 Dry run — a real run would make {len(ids)} billable Place Details call(s) "
              f"(Enterprise+Atmosphere tier) + {CALLS['search']} Search call(s). Nothing stored.")
        return

    # 2) Fetch full details for each and upsert the raw payload.
    saved = anchors = 0
    for i, pid in enumerate(ids, 1):
        payload = place_details(args.api_key, pid)
        if not payload:
            continue
        anchor = _is_anchor(payload, args.anchor)
        place_id = payload.get("id", pid)
        if conn is not None:
            _upsert(conn, place_id, label, anchor, payload)
        if raw_out:
            raw_out.write(json.dumps(
                {"place_id": place_id, "query": label, "is_anchor": anchor, "payload": payload},
                ensure_ascii=False) + "\n")
        saved += 1
        anchors += int(anchor)
        name = (payload.get("displayName") or {}).get("text", pid)
        nrev = len(payload.get("reviews", []) or [])
        print(f"   {i:>3}/{len(ids)} {'⭐' if anchor else '  '} {name}  ({nrev} reviews)")
        time.sleep(REQUEST_PAUSE_SEC)

    if conn is not None:
        conn.close()
    if raw_out:
        raw_out.close()
    sink = "places_raw" if conn is not None else os.path.basename(args.raw_out)
    print(f"\n✅ Saved {saved} businesses to {sink} ({anchors} anchor).")
    print(f"💳 Billable this run: {CALLS['search']} Search (Essentials) "
          f"+ {CALLS['details']} Place Details (Enterprise+Atmosphere). "
          f"Track against your monthly free allotment in Cloud Console → Metrics.")
    if args.anchor and not anchors:
        print(f"   ⚠️  no business matched anchor {args.anchor!r} — check the name/spelling.")


if __name__ == "__main__":
    main()
