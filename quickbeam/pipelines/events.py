"""
events.py — Event scrapers (Eventbrite organizers + The Events Calendar/Tribe)
            → Postgres `events_raw` (or a JSONL file).

The events twin of places.py. Where places.py sweeps Google Places, this sweeps
two free, public, no-API-key event sources and stores the verbatim payloads so
the downstream shaper (events_pg.py) can build the Fangorn graph — and reprocess
endlessly without re-fetching. Events are *merged* into the same local-business
graph as places: an Event happening at a venue we already have as a `Business`
gets linked to it (see events_pg.py).

Two sources behind one CLI (`--source`):

  eventbrite  An organizer profile, e.g. https://www.eventbrite.com/o/shotskis-29817730199
              Upcoming events come from the page's Next.js `__NEXT_DATA__` blob
              (full venue incl. lat/lng). Past events + pagination come from the
              internal JSON endpoint  /org/{id}/showmore/?type=past|future . Those
              rows omit the venue but carry `venue_id`, so we backfill venues from
              the upcoming events' venue map (an organizer reuses its venues).

  tribe       A WordPress "The Events Calendar" site, e.g. https://eagleriver.org
              Public REST API  /wp-json/tribe/events/v1/events  (paginated). Each
              event carries venue, organizer, categories and cost inline.

Storing the raw payload means reprocessing (schema tweaks, new node types) never
re-hits the network. Both sources are free; there is no per-call cost like the
Google Places SKU tiers — we just keep a fetch tally and stay polite.

ToS note: we store event *metadata* only (title, time, venue, price, organizer),
not third-party review text. Treat the cache as a short-lived prototype store.

Requires: requests; psycopg[binary] only for the (optional) Postgres path.
"""
import os
import re
import sys
import json
import time
import argparse

import requests

try:
    import psycopg
except ImportError:  # pragma: no cover - surfaced at runtime with a clear hint
    psycopg = None

DEFAULT_DSN = os.environ.get(
    "EVENTS_PG_DSN", "postgresql://places:places@localhost:5432/places_db"
)

# A real browser UA — eagleriver.org sits behind Cloudflare and rejects the
# default requests/curl UA, and Eventbrite is friendlier to one too.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/html"}
REQUEST_PAUSE_SEC = 0.4

EB_SHOWMORE = "https://www.eventbrite.com/org/{org_id}/showmore/"
EB_DISCOVERY = "https://www.eventbrite.com/d/{place}/all-events/"
TRIBE_PATH  = "/wp-json/tribe/events/v1/events"

FETCHED = {"http": 0, "events": 0}


# ===========================================================================
# POSTGRES (mirrors places.py: auto-created, idempotent upsert)
# ===========================================================================
DDL = """
CREATE TABLE IF NOT EXISTS events_raw (
    event_key   text PRIMARY KEY,
    source      text,
    organizer   text,
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


def _upsert(conn, event_key: str, source: str, organizer: str, payload: dict):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events_raw (event_key, source, organizer, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (event_key) DO UPDATE SET
                payload    = EXCLUDED.payload,
                fetched_at = now(),
                organizer  = COALESCE(EXCLUDED.organizer, events_raw.organizer)
            """,
            (event_key, source, organizer, json.dumps(payload)),
        )
    conn.commit()


# ===========================================================================
# EVENTBRITE
# ===========================================================================
def _eb_org_id(organizer: str) -> str:
    """Accept a full organizer URL or a slug; return the trailing numeric id.
    'shotskis-29817730199' / '.../o/shotskis-29817730199' -> '29817730199'."""
    m = re.search(r"(\d{6,})", organizer)
    if not m:
        raise SystemExit(f"Could not find an organizer id in {organizer!r}")
    return m.group(1)


def _eb_profile_url(organizer: str) -> str:
    if organizer.startswith("http"):
        return organizer
    return f"https://www.eventbrite.com/o/{organizer.strip('/')}"


def eb_next_data(profile_url: str) -> dict:
    """Fetch the organizer page and parse its Next.js __NEXT_DATA__ JSON blob."""
    FETCHED["http"] += 1
    resp = requests.get(profile_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text, re.S)
    if not m:
        raise SystemExit("No __NEXT_DATA__ on the organizer page — layout changed?")
    return json.loads(m.group(1)).get("props", {}).get("pageProps", {})


def eb_showmore(org_id: str, kind: str, max_events: int) -> list[dict]:
    """Page /org/{id}/showmore/?type=past|future until exhausted or capped."""
    out: list[dict] = []
    page = 1
    while len(out) < max_events:
        FETCHED["http"] += 1
        resp = requests.get(
            EB_SHOWMORE.format(org_id=org_id), headers=HEADERS,
            params={"type": kind, "page": page, "page_size": 50}, timeout=30)
        if resp.status_code != 200:
            print(f"   ⚠️  showmore {kind} p{page} {resp.status_code}: {resp.text[:160]}")
            break
        data = resp.json().get("data", {})
        evs = data.get("events", []) or []
        out.extend(evs)
        if not evs or not data.get("has_next_page"):
            break
        page += 1
        time.sleep(REQUEST_PAUSE_SEC)
    return out[:max_events]


def _server_data(html: str) -> dict | None:
    """Extract Eventbrite's `window.__SERVER_DATA__ = {...};` blob via brace match
    (a non-greedy regex breaks on the nested objects inside it)."""
    i = html.find("window.__SERVER_DATA__")
    if i < 0:
        return None
    start = html.find("{", i)
    depth = 0
    for j in range(start, len(html)):
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:j + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _eb_row(e: dict, org: dict, venue: dict) -> dict:
    """Build a stored row from an Eventbrite event dict (organizer/discovery shapes
    share the same event shape, so events_pg.normalize_eventbrite handles both)."""
    eid = str(e.get("id") or e.get("eid") or "")
    payload = dict(e)
    payload["_venue"] = venue
    payload["_organizer"] = org
    return {
        "event_key": f"eb:{eid}",
        "source": "eventbrite",
        "organizer": org.get("name") or org.get("id") or "",
        "payload": payload,
    }


def eb_discovery(place: str, max_events: int, bbox: str = "") -> list[dict]:
    """Eventbrite *location* discovery: page /d/{place}/all-events/ and read
    __SERVER_DATA__.search_data.events.results. Each event carries its venue (with
    coordinates) + primary_organizer_id, so the downstream coordinate match links
    it to whichever Business it happens at — no per-organizer ids needed.

    `bbox` (optional) is Eventbrite's geographic filter as
    "west_lng,south_lat,east_lng,north_lat". When set it overrides the `place`
    slug's geography, so any slug works as the path — pass coordinates to scrape
    a region outside the US (e.g. Honheim, DE) even though the slug says otherwise."""
    rows: list[dict] = []
    seen: set[str] = set()
    page, page_count = 1, 1
    while len(rows) < max_events and page <= page_count:
        FETCHED["http"] += 1
        params = {"page": page}
        if bbox:
            params["bbox"] = bbox
        resp = requests.get(EB_DISCOVERY.format(place=place), headers=HEADERS,
                            params=params, timeout=30)
        if resp.status_code != 200:
            print(f"   ⚠️  discovery p{page} {resp.status_code}: {resp.text[:160]}")
            break
        sd = _server_data(resp.text) or {}
        block = (sd.get("search_data") or {}).get("events") or {}
        results = block.get("results") or []
        page_count = (block.get("pagination") or {}).get("page_count") or 1
        for e in results:
            eid = str(e.get("id") or e.get("eid") or "")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            org = {"id": str(e.get("primary_organizer_id") or "")}
            rows.append(_eb_row(e, org, e.get("primary_venue") or {}))
        print(f"   discovery page {page}/{page_count}: +{len(results)} (have {len(rows)})")
        page += 1
        time.sleep(REQUEST_PAUSE_SEC)
    return rows[:max_events]

# purpose built for event brite data
def eb_expand_past(rows: list[dict], max_events: int) -> list[dict]:
    """For every distinct organizer discovered, pull their past events via the
    show-more endpoint (discovery is upcoming-only). Venues are backfilled from the
    organizers' events we already have."""
    org_ids = {r["payload"].get("_organizer", {}).get("id") for r in rows}
    org_ids = {o for o in org_ids if o}
    print(f"   expanding past events for {len(org_ids)} organizer(s)...")
    vmap = _eb_venue_map([r["payload"] for r in rows])
    extra: list[dict] = []
    seen = {r["event_key"] for r in rows}
    for oid in org_ids:
        for e in eb_showmore(oid, "past", max_events):
            eid = str(e.get("id") or "")
            if not eid or f"eb:{eid}" in seen:
                continue
            seen.add(f"eb:{eid}")
            venue = e.get("venue") or {}
            if not venue.get("address"):
                venue = vmap.get(str(e.get("venue_id") or ""), venue)
            extra.append(_eb_row(e, e.get("organizer") or {"id": oid}, venue))
        if len(rows) + len(extra) >= max_events:
            break
        time.sleep(REQUEST_PAUSE_SEC)
    print(f"   +{len(extra)} past event(s)")
    return extra


def _eb_venue_map(events: list[dict]) -> dict[str, dict]:
    """venue_id -> venue object, harvested from any events that carry a venue."""
    vmap: dict[str, dict] = {}
    for e in events:
        v = e.get("primary_venue") or e.get("venue") or {}
        vid = str(e.get("primary_venue_id") or e.get("venue_id") or v.get("id") or "")
        if vid and v.get("address"):
            vmap[vid] = v
    return vmap


def fetch_eventbrite(organizer: str, max_events: int) -> list[dict]:
    """Return enriched raw rows for an organizer: each row is
    {event_key, source, organizer, payload} where payload carries a resolved
    `_venue` and `_organizer` so the shaper needs no extra fetches."""
    profile_url = _eb_profile_url(organizer)
    org_id = _eb_org_id(organizer)
    print(f"🔎 Eventbrite organizer {org_id}  ({profile_url})")

    props = eb_next_data(profile_url)
    org = props.get("organizer", {}) or {}
    upcoming = props.get("upcomingEvents", []) or []
    print(f"   __NEXT_DATA__: {len(upcoming)} upcoming, "
          f"total={props.get('upcomingEventsTotal')}")

    future = eb_showmore(org_id, "future", max_events)
    past = eb_showmore(org_id, "past", max_events)
    print(f"   showmore: {len(future)} future, {len(past)} past")

    # Venue backfill: upcoming events (and any others) seed the venue_id map.
    vmap = _eb_venue_map(upcoming + future + past)

    rows: list[dict] = []
    seen: set[str] = set()
    for e in upcoming + future + past:
        eid = str(e.get("id") or e.get("eid") or "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        venue = e.get("primary_venue") or e.get("venue") or {}
        if not venue.get("address"):
            vid = str(e.get("primary_venue_id") or e.get("venue_id") or "")
            venue = vmap.get(vid, venue)
        payload = dict(e)
        payload["_venue"] = venue
        payload["_organizer"] = e.get("organizer") or org
        rows.append({
            "event_key": f"eb:{eid}",
            "source": "eventbrite",
            "organizer": org.get("name") or org_id,
            "payload": payload,
        })
    return rows


# ===========================================================================
# THE EVENTS CALENDAR (TRIBE)
# ===========================================================================
def fetch_tribe(site: str, max_events: int, start_date: str, end_date: str) -> list[dict]:
    site = site.rstrip("/")
    url = site + TRIBE_PATH
    print(f"🔎 Tribe calendar {url}")
    rows: list[dict] = []
    page = 1
    while len(rows) < max_events:
        FETCHED["http"] += 1
        params = {"per_page": 50, "page": page}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"   ⚠️  tribe p{page} {resp.status_code}: {resp.text[:160]}")
            break
        data = resp.json()
        evs = data.get("events", []) or []
        for e in evs:
            eid = str(e.get("id") or "")
            if not eid:
                continue
            org = (e.get("organizer") or [{}])
            org0 = org[0] if isinstance(org, list) and org else (org or {})
            rows.append({
                "event_key": f"tribe:{eid}",
                "source": "tribe",
                "organizer": (org0 or {}).get("organizer") or site,
                "payload": e,
            })
        total_pages = data.get("total_pages") or 1
        print(f"   page {page}/{total_pages}: +{len(evs)} (have {len(rows)})")
        if not evs or page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_PAUSE_SEC)
    return rows[:max_events]


# ===========================================================================
# CLI
# ===========================================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Scrape Eventbrite organizers / Tribe calendars into events_raw.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", choices=["eventbrite", "eventbrite-location", "tribe"],
                   required=True, help="Which scraper to run.")
    p.add_argument("--organizer", default="",
                   help='Eventbrite organizer slug or URL, e.g. "shotskis-29817730199".')
    p.add_argument("--place", default="",
                   help='eventbrite-location: discovery slug, e.g. "wi--eagle-river" '
                        '(from an eventbrite.com/d/<state>--<city>/ URL). '
                        'Defaults to "united-states" when --bbox is given.')
    p.add_argument("--bbox", default="",
                   help='eventbrite-location: geographic filter as '
                        '"west_lng,south_lat,east_lng,north_lat" (overrides the slug\'s '
                        'geography — use it to scrape non-US regions, e.g. '
                        '"8.817042,48.415394,9.617042,49.015394" for Honheim, DE).')
    p.add_argument("--expand-past", action="store_true", default=False,
                   help="eventbrite-location: also pull past events for every "
                        "discovered organizer (discovery itself is upcoming-only).")
    p.add_argument("--site", default="",
                   help='Tribe site base URL, e.g. "https://eagleriver.org".')
    p.add_argument("--start-date", default="",
                   help="Tribe: earliest event start (YYYY-MM-DD). Blank = API default.")
    p.add_argument("--end-date", default="",
                   help="Tribe: latest event start (YYYY-MM-DD). Blank = API default.")
    p.add_argument("--max-events", type=int, default=500,
                   help="Hard ceiling on events pulled per run.")
    p.add_argument("--dsn", default=DEFAULT_DSN,
                   help="Postgres connection string (or env EVENTS_PG_DSN).")
    p.add_argument("--no-db", action="store_true", default=False,
                   help="Skip Postgres entirely (requires --raw-out).")
    p.add_argument("--raw-out", default="",
                   help="Append each raw row to this JSONL file "
                        "(feed it to `eventspg --raw-in`).")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Fetch and report counts, but store nothing.")
    return p.parse_args()


def main():
    args = parse_args()
    if args.source == "eventbrite" and not args.organizer:
        raise SystemExit("--source eventbrite requires --organizer.")
    if args.source == "eventbrite-location" and not args.place and not args.bbox:
        raise SystemExit("--source eventbrite-location requires --place or --bbox.")
    if args.source == "tribe" and not args.site:
        raise SystemExit("--source tribe requires --site.")
    if args.no_db and not args.raw_out and not args.dry_run:
        raise SystemExit("--no-db requires --raw-out (nowhere to put the data otherwise).")

    if args.source == "eventbrite":
        rows = fetch_eventbrite(args.organizer, args.max_events)
    elif args.source == "eventbrite-location":
        place = args.place or "united-states"
        print(f"🔎 Eventbrite discovery: {place}"
              + (f"  bbox={args.bbox}" if args.bbox else ""))
        rows = eb_discovery(place, args.max_events, args.bbox)
        if args.expand_past:
            rows += eb_expand_past(rows, args.max_events)
    else:
        rows = fetch_tribe(args.site, args.max_events, args.start_date, args.end_date)
    FETCHED["events"] = len(rows)
    print(f"   collected {len(rows)} event row(s)")

    if args.dry_run:
        print(f"\n🔍 Dry run — {FETCHED['http']} HTTP request(s), {len(rows)} events. "
              f"Nothing stored.")
        return

    conn = None
    if not args.no_db:
        conn = _connect(args.dsn)
        print(f"🔌 Postgres: {args.dsn.rsplit('@', 1)[-1]}")
    raw_out = open(args.raw_out, "a", encoding="utf-8") if args.raw_out else None
    if raw_out:
        print(f"📄 Raw JSONL → {args.raw_out}")

    saved = 0
    for r in rows:
        if conn is not None:
            _upsert(conn, r["event_key"], r["source"], r["organizer"], r["payload"])
        if raw_out:
            raw_out.write(json.dumps(r, ensure_ascii=False) + "\n")
        saved += 1

    if conn is not None:
        conn.close()
    if raw_out:
        raw_out.close()
    sink = "events_raw" if conn is not None else os.path.basename(args.raw_out)
    print(f"\n✅ Saved {saved} events to {sink} "
          f"({FETCHED['http']} HTTP requests, source={args.source}).")


if __name__ == "__main__":
    main()
