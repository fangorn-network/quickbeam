"""
events_pg.py — events_raw (Eventbrite/Tribe payloads) → Fangorn graph,
               MERGED into the local-business (places) graph.

The events twin of places_pg.py. It normalizes the two scraper payload shapes
(events.py) into one common event, then emits node/edge volume files in the SAME
`stage_volumes/` directory as places_pg — so a single `schemagen → build → cdn`
pass produces one shard carrying both Businesses and Events.

  Nodes : Event, Organizer  (+ reuses Category, Locality from the places graph)
  Edges : hostedBy   (Event→Organizer)
          inCategory (Event→Category)
          locatedIn  (Event→Locality)
          hostedAt   (Event→Business)   ← the merge link, by coordinate match
          hostsEvent (Business→Event)   ← reverse, so a bar lists its events

Default output volume is 2, so it coexists with the places `volume_1_*` files
(schemagen reads every `volume_*.json` in the directory). The merge link resolves
each Event's venue to the nearest existing `Business` (loaded from
`volume_1_businesses.json`) within `--match-radius-m`, falling back to a venue-
name/business-title match. Events without a match still stand alone.

Every node carries a verbalized `text` field — that's what gets embedded.

Requires: psycopg[binary] only for the (optional) Postgres path.
"""
import os
import re
import json
import math
import argparse
import hashlib
from datetime import datetime, date

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

SCHEMA_VERSION = 1
DEFAULT_DSN = os.environ.get(
    "EVENTS_PG_DSN", "postgresql://places:places@localhost:5432/places_db"
)
TODAY = date.today()

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ===========================================================================
# OUTPUT (same streaming writer / helpers as places_pg.py)
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


def _haversine_m(a: tuple, b: tuple) -> float:
    (lat1, lon1), (lat2, lon2) = a, b
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _strip_html(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&amp;", "&").replace("&#8217;", "’").replace("&#8211;", "–")
          .replace("&nbsp;", " ").replace("&quot;", '"').replace("&#039;", "'"))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _fnum(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# NORMALIZE — Eventbrite / Tribe payload → one common event dict
# ===========================================================================
def _eb_name(payload: dict) -> str:
    n = payload.get("name")
    if isinstance(n, dict):
        return n.get("text") or "(untitled event)"
    return n or "(untitled event)"


def _eb_datetime(payload: dict, which: str) -> tuple[str | None, str | None, str | None, str | None]:
    """(iso_local, date, time, timezone) from an Eventbrite event (both shapes)."""
    obj = payload.get(which)
    if isinstance(obj, dict):  # showmore shape: start/end objects
        local = obj.get("local")
        tz = obj.get("timezone")
        if local:
            d, _, t = local.partition("T")
            return local, d, (t[:5] if t else None), tz
    # __NEXT_DATA__ shape: flat start_date / start_time
    d = payload.get(f"{which}_date")
    t = payload.get(f"{which}_time")
    tz = payload.get("timezone")
    if d:
        iso = f"{d}T{t}" if t else d
        return iso, d, (t[:5] if t else None), tz
    return None, None, None, tz


def _eb_price(payload: dict) -> tuple[float | None, float | None, bool, str | None, str | None]:
    """(min, max, is_free, currency, display)."""
    ta = payload.get("ticket_availability") or {}
    is_free = bool(ta.get("is_free") or payload.get("is_free"))
    currency = payload.get("currency")
    mn = mx = None
    if ta:
        mn = _fnum((ta.get("minimum_ticket_price") or {}).get("major_value"))
        mx = _fnum((ta.get("maximum_ticket_price") or {}).get("major_value"))
    pr = payload.get("price_range")  # e.g. "$52.61 - $86.38"
    if pr and (mn is None or mx is None):
        nums = [float(x) for x in re.findall(r"[\d.]+", pr)]
        if nums:
            mn = mn if mn is not None else min(nums)
            mx = mx if mx is not None else max(nums)
    display = ("Free" if is_free else
               (pr if pr else (f"From ${mn:g}" if mn is not None else None)))
    return mn, mx, is_free, currency, display


def normalize_eventbrite(payload: dict) -> dict:
    venue = payload.get("_venue") or {}
    addr = venue.get("address") or {}
    org = payload.get("_organizer") or {}
    iso, sdate, stime, tz = _eb_datetime(payload, "start")
    _, edate, _, _ = _eb_datetime(payload, "end")
    mn, mx, is_free, currency, display = _eb_price(payload)
    cat = payload.get("category") or {}
    img = (payload.get("image") or payload.get("logo") or {})
    return {
        "source": "eventbrite",
        "event_id": str(payload.get("id") or ""),
        "title": _eb_name(payload),
        "ticket_url": payload.get("url"),
        "start_iso": iso, "start_date": sdate, "start_time": stime,
        "end_date": edate, "timezone": tz, "all_day": False,
        "is_cancelled": bool(payload.get("is_cancelled")),
        "is_online": bool(payload.get("is_online_event") or payload.get("online_event")),
        "venue_name": venue.get("name"),
        "address": addr.get("localized_address_display"),
        "lat": _fnum(addr.get("latitude")), "lng": _fnum(addr.get("longitude")),
        "city": addr.get("city"), "region": addr.get("region"),
        "postal": addr.get("postal_code"),
        "price_min": mn, "price_max": mx, "is_free": is_free,
        "currency": currency, "price_display": display,
        "organizer_id": str(org.get("id") or ""),
        "organizer_name": org.get("name"),
        "organizer_website": org.get("website") or (org.get("socials") or {}).get("website"),
        "organizer_facebook": org.get("facebook") or (org.get("socials") or {}).get("facebook"),
        "organizer_bio": _strip_html(org.get("bio")),
        "categories": [cat["name"]] if cat.get("name") else [],
        "summary": _strip_html(payload.get("summary")),
        "image_url": img.get("url"),
    }


def normalize_tribe(payload: dict) -> dict:
    venue = payload.get("venue") or {}
    org_list = payload.get("organizer") or []
    org = org_list[0] if isinstance(org_list, list) and org_list else (org_list or {})
    sd = (payload.get("start_date") or "")
    ed = (payload.get("end_date") or "")
    sdate, _, stime = sd.partition(" ")
    edate = ed.partition(" ")[0]
    cost = payload.get("cost") or ""
    nums = [float(x) for x in re.findall(r"[\d.]+", cost)]
    return {
        "source": "tribe",
        "event_id": str(payload.get("id") or ""),
        "title": _strip_html(payload.get("title")),
        "ticket_url": payload.get("url"),
        "start_iso": sd.replace(" ", "T") or None,
        "start_date": sdate or None, "start_time": (stime[:5] if stime else None),
        "end_date": edate or None, "timezone": payload.get("timezone"),
        "all_day": bool(payload.get("all_day")),
        "is_cancelled": payload.get("status") == "cancelled",
        "is_online": bool(payload.get("is_virtual")),
        "venue_name": venue.get("venue"),
        "address": venue.get("address"),
        "lat": _fnum(venue.get("geo_lat")), "lng": _fnum(venue.get("geo_lng")),
        "city": venue.get("city"), "region": venue.get("state"),
        "postal": venue.get("zip"),
        "price_min": (min(nums) if nums else None),
        "price_max": (max(nums) if nums else None),
        "is_free": (not cost) or cost.strip().lower() in ("free", "$0", "0"),
        "currency": "USD", "price_display": (cost or "Free"),
        "organizer_id": str((org or {}).get("id") or ""),
        "organizer_name": (org or {}).get("organizer"),
        "organizer_website": (org or {}).get("website"),
        "organizer_facebook": (org or {}).get("facebook"),
        "organizer_bio": None,
        "categories": [c.get("name") for c in (payload.get("categories") or []) if c.get("name")],
        "summary": _strip_html(payload.get("excerpt") or payload.get("description")),
        "image_url": (payload.get("image") or {}).get("url"),
    }


# ===========================================================================
# SHAPING — common event → node fields
# ===========================================================================
def _pretty_date(ev: dict) -> str | None:
    iso = ev.get("start_iso") or ev.get("start_date")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso[:19])
    except ValueError:
        return ev.get("start_date")
    s = f"{WEEKDAYS[dt.weekday()]} {MONTHS[dt.month]} {dt.day}, {dt.year}"
    if not ev.get("all_day") and (dt.hour or dt.minute):
        h = dt.hour % 12 or 12
        s += f" at {h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"
    return s


def _is_past(ev: dict) -> bool:
    ref = ev.get("end_date") or ev.get("start_date")
    if not ref:
        return False
    try:
        return date.fromisoformat(ref[:10]) < TODAY
    except ValueError:
        return False


def shape_event(ev: dict) -> dict:
    coords = (f"{ev['lat']},{ev['lng']}" if ev.get("lat") is not None
              and ev.get("lng") is not None else None)
    locality = ", ".join(p for p in (ev.get("city"), ev.get("region")) if p) or None
    pretty = _pretty_date(ev)
    is_past = _is_past(ev)

    price = ("Free" if ev.get("is_free") else ev.get("price_display"))
    text = (f"{ev['title']} — "
            + ("past event" if is_past else "upcoming event")
            + (f" at {ev['venue_name']}" if ev.get("venue_name") else "")
            + (f" in {locality}" if locality else "")
            + (f" on {pretty}" if pretty else "")
            + (f", hosted by {ev['organizer_name']}" if ev.get("organizer_name") else "")
            + (f". {price}" if price and price != "Free" else (". Free admission" if ev.get("is_free") else ""))
            + (f". {', '.join(ev['categories'])}" if ev.get("categories") else "")
            + (f". {ev['summary']}" if ev.get("summary") else ""))

    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Event",
        "eventId": ev["event_id"], "title": ev["title"],
        "startDate": ev.get("start_date"), "startTime": ev.get("start_time"),
        "endDate": ev.get("end_date"), "startISO": ev.get("start_iso"),
        "dateLabel": pretty, "timezone": ev.get("timezone"),
        "isPast": is_past, "isCancelled": ev.get("is_cancelled") or None,
        "isOnline": ev.get("is_online") or None,
        "venueName": ev.get("venue_name"), "address": ev.get("address"),
        "coordinates": coords, "locality": locality,
        "priceMin": ev.get("price_min"), "priceMax": ev.get("price_max"),
        "isFree": ev.get("is_free") or None, "priceLevel": price,
        "ticketUrl": ev.get("ticket_url"),
        "organizerName": ev.get("organizer_name"),
        "categories": ev.get("categories") or None,
        "imageUrl": ev.get("image_url"),
        "source": ev["source"], "summary": ev.get("summary"),
        "text": text,
    })


def _org_key(ev: dict) -> str | None:
    oid, name = ev.get("organizer_id"), ev.get("organizer_name")
    if not (oid or name):
        return None
    base = oid or hashlib.sha1((name or "").encode()).hexdigest()[:12]
    return f"{ev['source']}-org-{base}"


def shape_organizer(ev: dict) -> dict:
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Organizer",
        "organizerId": _org_key(ev), "title": ev.get("organizer_name"),
        "bio": ev.get("organizer_bio"), "website": ev.get("organizer_website"),
        "facebook": ev.get("organizer_facebook"), "source": ev["source"],
        "text": (f"{ev.get('organizer_name')} — event organizer"
                 + (f". {ev['organizer_bio']}" if ev.get("organizer_bio") else "")),
    })


def shape_category(name: str) -> dict:
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Category",
        "categoryId": _slug(name), "title": name,
        "text": f"{name} — event category",
    })


def shape_locality(slug: str, title: str, region: str | None) -> dict:
    return _clean({
        "schemaVersion": SCHEMA_VERSION, "entityType": "Locality",
        "localityId": slug, "title": title, "region": region,
        "text": f"{title} — locality",
    })


# ===========================================================================
# BUSINESS INDEX (for the hostedAt merge link)
# ===========================================================================
def load_businesses(path: str) -> list[dict]:
    """[{place_id, title, coords:(lat,lng)|None}] from a places volume file."""
    if not path or not os.path.exists(path):
        return []
    out = []
    for rec in json.load(open(path, encoding="utf-8")):
        f = rec.get("fields", {}) or {}
        coords = None
        c = f.get("coordinates")
        if isinstance(c, str) and "," in c:
            try:
                lat, lng = (float(x) for x in c.split(","))
                coords = (lat, lng)
            except ValueError:
                coords = None
        out.append({"place_id": rec.get("name"),
                    "title": str(f.get("title") or "").lower(),
                    "title_display": f.get("title"), "coords": coords})
    return out


def match_business(ev: dict, businesses: list[dict], radius_m: float) -> dict | None:
    """Nearest business within radius by coordinate, else venue-name == title.
    Returns the matched business dict (place_id, title_display, …) or None."""
    if ev.get("lat") is not None and ev.get("lng") is not None:
        origin = (ev["lat"], ev["lng"])
        best, best_d = None, radius_m
        for b in businesses:
            if b["coords"]:
                d = _haversine_m(origin, b["coords"])
                if d <= best_d:
                    best, best_d = b, d
        if best:
            return best
    vn = (ev.get("venue_name") or "").lower().strip()
    if vn:
        for b in businesses:
            if b["title"] and (vn == b["title"] or vn in b["title"] or b["title"] in vn):
                return b
    return None


# ===========================================================================
# RAW SOURCE — Postgres or JSONL (rows: {source, payload})
# ===========================================================================
def iter_db_rows(conn):
    with conn.cursor(name="events_stream", row_factory=psycopg.rows.dict_row) as cur:
        cur.itersize = 1000
        cur.execute("SELECT source, payload FROM events_raw")
        yield from cur


def iter_jsonl_rows(path: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            yield {"source": rec.get("source"),
                   "payload": rec.get("payload", rec)}


# ===========================================================================
# EXTRACTION
# ===========================================================================
def run_export(rows, out_dir: str, volume: int, businesses: list[dict], radius_m: float):
    paths = {t: os.path.join(out_dir, f"volume_{volume}_{stem}.json")
             for t, stem in [("Event", "events"), ("Organizer", "organizers"),
                             ("Category", "event_categories"),
                             ("Locality", "event_localities")]}
    writers = {t: JsonArrayWriter(p) for t, p in paths.items()}
    edges = JsonArrayWriter(os.path.join(out_dir, f"volume_{volume}_edges.json"))

    seen_org: set[str] = set()
    seen_cat: set[str] = set()
    seen_loc: set[str] = set()
    n_linked = 0

    def edge(rel, frm, to, ft, tt, **extra):
        edges.write(_clean({"rel": rel, "from": frm, "to": to,
                            "fromType": ft, "toType": tt, **extra}))

    for row in rows:
        ev = (normalize_eventbrite if row["source"] == "eventbrite"
              else normalize_tribe)(row["payload"])
        if not ev.get("event_id"):
            continue
        ekey = f"{ev['source']}:{ev['event_id']}"

        # Resolve the host Business first so we can stamp it onto the Event node
        # (fields.hostBusinessId / hostBusinessName) — this is what lets the bar
        # page query its events live and the event page link back to the bar,
        # with ids that stay correct in both qdrant and shards modes.
        biz = match_business(ev, businesses, radius_m)
        fields = shape_event(ev)
        if biz:
            fields["hostBusinessId"] = biz["place_id"]
            if biz.get("title_display"):
                fields["hostBusinessName"] = biz["title_display"]
        writers["Event"].write({"name": ekey, "fields": fields})

        # Organizer
        okey = _org_key(ev)
        if okey:
            if okey not in seen_org:
                seen_org.add(okey)
                writers["Organizer"].write({"name": okey, "fields": shape_organizer(ev)})
            edge("hostedBy", ekey, okey, "Event", "Organizer")

        # Categories
        for cname in ev.get("categories") or []:
            cslug = _slug(cname)
            if cslug not in seen_cat:
                seen_cat.add(cslug)
                writers["Category"].write({"name": cslug, "fields": shape_category(cname)})
            edge("inCategory", ekey, cslug, "Event", "Category")

        # Locality
        if ev.get("city") or ev.get("region"):
            title = ", ".join(p for p in (ev.get("city"), ev.get("region")) if p)
            lslug = _slug(ev.get("city") or "", ev.get("region") or "")
            if lslug not in seen_loc:
                seen_loc.add(lslug)
                writers["Locality"].write(
                    {"name": lslug, "fields": shape_locality(lslug, title, ev.get("region"))})
            edge("locatedIn", ekey, lslug, "Event", "Locality")

        # hostedAt / hostsEvent — the merge link to an existing Business.
        if biz:
            edge("hostedAt", ekey, biz["place_id"], "Event", "Business")
            edge("hostsEvent", biz["place_id"], ekey, "Business", "Event")
            n_linked += 1

    for t, w in writers.items():
        w.close()
        print(f"   ✅ {t:<9}: {w.count:,} → {os.path.basename(paths[t])}")
    edges.close()
    print(f"   ✅ edges    : {edges.count:,}")
    print(f"   🔗 hostedAt : {n_linked} event(s) linked to a Business "
          f"(of {len(businesses)} businesses)")


# ===========================================================================
# CLI
# ===========================================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Convert events_raw (Eventbrite/Tribe) into a Fangorn graph "
                    "merged with the places graph.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres connection (or env EVENTS_PG_DSN).")
    p.add_argument("--raw-in", default="", help="Read raw rows from this JSONL file instead of Postgres.")
    p.add_argument("--output-dir", default="./stage_volumes")
    p.add_argument("--volume", type=int, default=2,
                   help="Volume number (2 coexists with the places volume_1_*).")
    p.add_argument("--businesses-in", default="",
                   help="places volume file for the hostedAt merge link "
                        "(default: <output-dir>/volume_1_businesses.json if present).")
    p.add_argument("--match-radius-m", type=float, default=120.0,
                   help="Link an Event to the nearest Business within this many metres.")
    return p.parse_args()


def run():
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    biz_path = args.businesses_in or os.path.join(args.output_dir, "volume_1_businesses.json")
    businesses = load_businesses(biz_path)
    if businesses:
        print(f"🔗 Business index: {len(businesses)} from {os.path.basename(biz_path)}")
    else:
        print("🔗 Business index: none (events will stand alone — no hostedAt edges)")

    if args.raw_in:
        print(f"📄 Source: {args.raw_in}")
        rows = iter_jsonl_rows(args.raw_in)
        run_export(rows, args.output_dir, args.volume, businesses, args.match_radius_m)
    else:
        if psycopg is None:
            raise SystemExit("psycopg not installed. Run: pip install 'psycopg[binary]' "
                             "(or use --raw-in to skip Postgres).")
        print(f"🔌 Connecting: {args.dsn.rsplit('@', 1)[-1]}")
        with psycopg.connect(args.dsn) as conn:
            run_export(iter_db_rows(conn), args.output_dir, args.volume,
                       businesses, args.match_radius_m)
    print("\n📊 Done. Next: quickbeam data schemagen --input-dir ./stage_volumes "
          "--prefix fangorn.places --bundle-name localcore --version v1")


if __name__ == "__main__":
    run()
