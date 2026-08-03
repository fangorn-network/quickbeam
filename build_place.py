#!/usr/bin/env python3
"""
Interactive builder for a Local Discovery semantic-search shard for ANY place.

Wraps the quickbeam pipeline end to end:
    geocode -> scrape (Places + Events) -> graph -> embed -> bake -> demo

Run from the repo root with the venv active:
    python build_place.py

Nothing is billable until you confirm after the Places dry-run. The only step
that ever costs money is Google Place Details (~$0.025 per unique business);
all Search calls are free. See jackson.txt for the full background.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

QDRANT = os.environ.get("QDRANT_URL", "http://localhost:6333")
DIM = 256                      # nomic-embed-text-v1.5 matryoshka-256 (must match bake/build)
STAGE_DIR = "./stage_volumes"
CDN_DIR = "./cdn"

# Validated Google Places (new) Table A type presets. Nearby Search accepts <= 50.
TYPE_PRESETS = {
    "wide": (
        "restaurant,cafe,coffee_shop,bar,pub,wine_bar,bakery,ice_cream_shop,"
        "meal_takeaway,fast_food_restaurant,store,book_store,clothing_store,"
        "shoe_store,jewelry_store,gift_shop,thrift_store,electronics_store,"
        "furniture_store,home_goods_store,hardware_store,sporting_goods_store,"
        "pet_store,liquor_store,convenience_store,grocery_store,supermarket,"
        "department_store,shopping_mall,florist,art_gallery,museum,art_museum,"
        "history_museum,cultural_center,performing_arts_theater,concert_hall,"
        "movie_theater,night_club,tourist_attraction,hair_care,beauty_salon,"
        "barber_shop,spa,gym,park"
    ),
    "everyday": (
        "store,restaurant,cafe,coffee_shop,bar,bakery,grocery_store,supermarket,"
        "convenience_store,clothing_store,electronics_store,home_goods_store,"
        "shopping_mall,pharmacy,bank,gas_station,hair_care,beauty_salon,gym,"
        "book_store,hardware_store,liquor_store,pet_store,florist,park"
    ),
    "nightlife": (
        "restaurant,bar,night_club,pub,wine_bar,cafe,coffee_shop,bakery,"
        "ice_cream_shop,fast_food_restaurant,movie_theater,performing_arts_theater,"
        "concert_hall,museum,art_gallery,tourist_attraction,park,bowling_alley,"
        "casino,amusement_center"
    ),
}

# ----------------------------------------------------------------------------- helpers

def c(txt, code):  # tiny ANSI colorizer
    return f"\033[{code}m{txt}\033[0m" if sys.stdout.isatty() else txt

def hdr(txt):
    print("\n" + c("== " + txt + " ", "1;36") + c("=" * max(0, 60 - len(txt)), "36"))

def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    val = input(c(f"? {prompt}{suffix}: ", "33")).strip()
    return val or default

def ask_yes_no(prompt, default=True):
    d = "Y/n" if default else "y/N"
    val = input(c(f"? {prompt} [{d}]: ", "33")).strip().lower()
    if not val:
        return default
    return val.startswith("y")

def die(msg):
    print(c("✗ " + msg, "31"))
    sys.exit(1)

def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def find_quickbeam():
    qb = shutil.which("quickbeam")
    if qb:
        return [qb]
    local = os.path.join(os.getcwd(), "venv", "bin", "quickbeam")
    if os.path.exists(local):
        return [local]
    die("`quickbeam` not found. Activate the venv first: source venv/bin/activate")

QB = None  # set in main()

def run(args, *, dry_label=None):
    """Run a quickbeam subcommand (args = list after 'quickbeam'). Streams output."""
    cmd = QB + args
    print(c("\n$ " + " ".join(_show(a) for a in cmd), "90"))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        if not ask_yes_no(c(f"  ↑ command exited {proc.returncode}. Continue anyway?", "31"), False):
            die("aborted")
    return proc.returncode

def _show(a):  # quote args with spaces for the echoed command line
    return f'"{a}"' if (" " in a and not a.startswith("--")) else a

# ----------------------------------------------------------------------------- preflight

def preflight(want_places):
    hdr("Preflight")
    global QB
    QB = find_quickbeam()
    print(c("✓", "32"), "quickbeam:", QB[0])

    try:
        with urllib.request.urlopen(f"{QDRANT}/collections", timeout=5) as r:
            json.load(r)
        print(c("✓", "32"), "Qdrant reachable at", QDRANT)
    except Exception:
        die(f"Qdrant not reachable at {QDRANT}. Start it:\n"
            "  docker run -d -p 6333:6333 -p 6334:6334 --name qdrant-core qdrant/qdrant")

    if not os.environ.get("PLACES_PG_DSN"):
        print(c("!", "33"), "PLACES_PG_DSN not set — placespg/eventspg will use the default DSN.\n"
              "    Ensure Postgres is up, or this run will fail at Stage B.")
    if want_places and not os.environ.get("GOOGLE_PLACES_API_KEY"):
        die("GOOGLE_PLACES_API_KEY not set, but you chose to scrape places.\n"
            "  export GOOGLE_PLACES_API_KEY=AIza...   (Places API new + billing on)")

# ----------------------------------------------------------------------------- geocode

def geocode(place):
    """Free OSM Nominatim lookup -> (lat, lng, bbox_eventbrite, address dict)."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": place, "format": "json", "limit": 1, "addressdetails": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "quickbeam-build-place/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except Exception as e:
        print(c(f"  geocode failed ({e}); enter coordinates manually.", "31"))
        return None
    if not data:
        print(c("  no geocoding match; enter coordinates manually.", "31"))
        return None
    hit = data[0]
    lat, lng = float(hit["lat"]), float(hit["lon"])
    # Nominatim boundingbox = [south, north, west, east]; Eventbrite wants W,S,E,N
    s, n, w, e = (float(x) for x in hit["boundingbox"])
    bbox = f"{w},{s},{e},{n}"
    return lat, lng, bbox, hit.get("address", {}), hit.get("display_name", place)

# ----------------------------------------------------------------------------- Qdrant collection

def ensure_collection(name):
    """Create the collection (size=DIM, Cosine, on_disk) if absent; offer reset if present."""
    exists = False
    try:
        with urllib.request.urlopen(f"{QDRANT}/collections/{name}", timeout=5) as r:
            exists = r.status == 200
    except Exception:
        exists = False

    if exists:
        if ask_yes_no(f"Collection '{name}' exists. RESET it (delete + recreate)?", False):
            _qdrant("DELETE", f"/collections/{name}")
            exists = False
        else:
            print(c("  reusing existing collection (points will upsert).", "90"))
            return
    body = {"vectors": {"size": DIM, "distance": "Cosine", "on_disk": True}}
    _qdrant("PUT", f"/collections/{name}", body)
    print(c("✓", "32"), f"collection '{name}' ready (size={DIM}, Cosine).")

def _qdrant(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(QDRANT + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception as e:
        print(c(f"  Qdrant {method} {path} -> {e}", "31"))

# ----------------------------------------------------------------------------- main

def main():
    print(c("\n  Local Discovery shard builder — any place, interactive\n", "1;35"))

    # --- choose stages up front
    do_places = ask_yes_no("Scrape Google Places (businesses)? (needs API key, costs money)", True)
    do_events = ask_yes_no("Scrape events (Eventbrite, free)?", True)

    preflight(do_places)

    # --- location
    hdr("Location")
    place = ask("Place name (e.g. 'Jackson, MS' or 'Lisbon, Portugal')", "Jackson, MS")
    geo = geocode(place)
    lat = lng = bbox = None
    addr = {}
    if geo:
        lat, lng, bbox, addr, display = geo
        print(c("  resolved:", "32"), display)
        print(f"    center : {lat:.5f},{lng:.5f}")
        print(f"    bbox   : {bbox}   (W,S,E,N — used for events)")
        if not ask_yes_no("  Use these?", True):
            geo = None
    if not geo:
        coords = ask("Enter center as 'lat,lng'", "")
        if not re.match(r"^-?\d+\.?\d*,-?\d+\.?\d*$", coords or ""):
            die("need a valid 'lat,lng'")
        lat, lng = (float(x) for x in coords.split(","))
        bbox = ask("Enter events bbox 'W,S,E,N' (blank to skip events)", "") or None
        if do_events and not bbox:
            do_events = False

    # --- community labels for the demo UI (best-effort from geocoding)
    region = addr.get("state") or addr.get("region") or addr.get("country") or ""
    iso = addr.get("ISO3166-2-lvl4", "")          # e.g. "US-MS"
    region_abbr = iso.split("-")[-1] if "-" in iso else ""
    comm_name = addr.get("city") or addr.get("town") or addr.get("village") \
        or place.split(",")[0].strip()

    # --- target collection / cdn
    hdr("Output targets")
    collection = ask("Qdrant collection name", "fangorn")
    cdn_dir = ask("CDN output dir", CDN_DIR)
    domain = ask("Domain name to bake (filters to Business+Event)", "places")

    # --- places options
    place_args = None
    text_queries = []
    if do_places:
        hdr("Places — type net & sweep")
        print("  presets: " + ", ".join(TYPE_PRESETS) + ", or 'custom'")
        choice = ask("Type net", "wide").lower()
        if choice in TYPE_PRESETS:
            types = TYPE_PRESETS[choice]
        else:
            types = ask("Comma-separated Google types", TYPE_PRESETS["wide"])
        ntypes = len([t for t in types.split(",") if t.strip()])
        if ntypes > 50:
            print(c(f"  ! {ntypes} types > Google's 50-per-call limit; trimming to first 50.", "33"))
            types = ",".join(types.split(",")[:50])

        radius = ask("Sweep radius (meters)", "12000")
        min_radius = ask("Min tile radius (lower = digs deeper into dense areas)", "250")
        max_tiles = ask("Max search tiles (cheap calls)", "600")
        max_details = ask("Max billable Place Details (HARD spend ceiling)", "500")
        place_args = ["data", "places-fetch", "--location", f"{lat},{lng}",
                      "--radius", radius, "--types", types, "--sweep",
                      "--min-radius", min_radius, "--max-tiles", max_tiles,
                      "--max-details", max_details]

        hdr("Places — text passes (catch niche, type-less shops)")
        print("  Google has NO record_store/antique/tattoo type — text search is how")
        print("  you catch them. Edit the list, or leave blank to skip.")
        default_q = "; ".join(
            f"{kind} in {place}" for kind in
            ["record stores", "vintage and antique shops", "art galleries",
             "independent bookstores", "live music venues"])
        raw = ask("Text queries (';'-separated)", default_q)
        text_queries = [q.strip() for q in raw.split(";") if q.strip()]

    # --- fresh start?
    hdr("Workspace")
    if os.path.isdir(STAGE_DIR) and any(f.startswith("volume_") for f in os.listdir(STAGE_DIR)):
        if ask_yes_no(f"{STAGE_DIR} has existing volume_*.json (possibly another place). "
                      "Clear it for a clean shard?", True):
            for f in os.listdir(STAGE_DIR):
                if f.startswith("volume_"):
                    os.remove(os.path.join(STAGE_DIR, f))
            print(c("  cleared.", "90"))

    # --- summary + go
    hdr("Plan")
    print(f"  place        : {place}  ({lat:.4f},{lng:.4f})")
    print(f"  community    : {comm_name} / {region} ({region_abbr or '?'})")
    print(f"  collection   : {collection}   cdn: {cdn_dir}   domain: {domain}")
    print(f"  scrape places: {do_places}" + (f"   text passes: {len(text_queries)}" if do_places else ""))
    print(f"  scrape events: {do_events}" + (f"   bbox: {bbox}" if do_events else ""))
    if not ask_yes_no("Proceed?", True):
        die("aborted by user")

    # ====================================================================== STAGE A
    if do_places:
        hdr("Stage A1 — Places dry-run (no charge, no storage)")
        run(place_args + ["--dry-run"])
        if not ask_yes_no(c("This is the ONLY billable step. Run the REAL Places scrape now?", "1;31"), False):
            print(c("  skipping the billable run; continuing with whatever is already cached.", "90"))
        else:
            run(place_args)
            for q in text_queries:
                run(["data", "places-fetch", "--query", q, "--max-results", "60"])

    if do_events and bbox:
        hdr("Stage A2 — Events (free)")
        # NOTE: '--bbox=' equals-form so a leading '-' isn't read as a flag.
        run(["data", "events-fetch", "--source", "eventbrite-location",
             f"--bbox={bbox}", "--max-events", "500"])

    # ====================================================================== STAGE B
    hdr("Stage B — raw store -> graph")
    if do_places:
        run(["data", "placespg", "--output-dir", STAGE_DIR])
    if do_events:
        run(["data", "eventspg", "--output-dir", STAGE_DIR])

    # ====================================================================== STAGE C
    hdr("Stage C — schema, collection, embeddings, shard")
    run(["data", "schemagen", "--input-dir", STAGE_DIR, "--volume", "0",
         "--prefix", "fangorn.places", "--bundle-name", "localcore", "--version", "v1"])

    ensure_collection(collection)

    # businesses FIRST (events fold into existing business payloads)
    run(["data", "prebake", "--input-dir", STAGE_DIR, "--volume", "1",
         "--types", "Business", "--collection", collection])
    if do_events:
        run(["data", "prebake", "--input-dir", STAGE_DIR, "--volume", "2",
             "--types", "Event,Organizer", "--collection", collection, "--link-events"])

    run(["cdn", "bake", "--collection", collection, "--domain", domain, "--cdn-dir", cdn_dir])

    # ====================================================================== DONE
    hdr("Done")
    try:
        man = json.load(open(os.path.join(cdn_dir, domain, "manifest.json")))
        print(c("✓", "32"), f"baked {man.get('count')} points:",
              [(e['type'], e['count']) for e in man.get('entity_types', [])])
    except Exception:
        pass

    slug = slugify(comm_name or place)
    print("\nLaunch the demo (two terminals):\n")
    print(c(f"  quickbeam cdn serve --cdn-dir {cdn_dir} --port 8090 --cors", "36"))
    env = (f'VITE_DATA_SOURCE=shards VITE_CDN_URL=http://localhost:8090 VITE_DOMAIN={domain} '
           f'VITE_COMMUNITY_NAME="{comm_name}" VITE_COMMUNITY_REGION="{region}" '
           f'VITE_COMMUNITY_REGION_ABBR="{region_abbr}" VITE_COMMUNITY_SLUG="{slug}"')
    print(c(f"  cd examples && {env} npm run dev", "36"))
    print("\nThen open the URL it prints (http://localhost:5173, or 5174 if taken).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(c("\naborted", "31"))
        sys.exit(130)
