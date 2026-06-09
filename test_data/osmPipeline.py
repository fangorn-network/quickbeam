"""
osmPipeline.py — OpenStreetMap changeset ingest for Fangorn.

Proves the thesis that adding a new domain is a *schema change, not an
architecture change*: this mirrors mbPipeline.py but emits records under an
`osm_changeset` schema instead of music. The ingest server + app then infer the
semantic roles automatically (title←comment, subtitle←user_id, tags←tags,
spatial←bbox, temporal←created_at, measures←num_changes — no media → no player).

Output: ./stage_volumes/osm_changesets.json — a JSON array of
{"name": <changeset_id>, "fields": {...}} records, the same shape the SDK
ingest path consumes (see mbPipeline core/taxonomy files).

No third-party deps — stdlib only (urllib + xml.etree). Run:
    python osmPipeline.py
Then register the schema + publish via the Fangorn SDK and point the server at it:
    python server.py --schema osm_changeset=0x<id> --reset
"""

import os
import json
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ===========================================================================
# CONFIG
# ===========================================================================
# DFW metro bounding box: (min_lon, min_lat, max_lon, max_lat)
BBOX               = (-97.5, 32.5, -96.5, 33.2)
TARGET_COUNT       = 2000
DAYS_BACK          = 30
OUTPUT_DIR         = "./stage_volumes"
OUTPUT_PATH        = os.path.join(OUTPUT_DIR, "osm_changesets.json")
OSM_API            = "https://api.openstreetmap.org/api/0.6/changesets"
USER_AGENT         = "Fangorn-OSM-Pipeline/1.0 (https://fangorn.network)"
REQUEST_PAUSE_SEC  = 1.0   # be polite to the public API

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ===========================================================================
# FETCH — walk backwards one day at a time; each query returns up to 100
# changesets for that day within the bounding box.
# ===========================================================================
def _fetch_day(day_start: datetime, day_end: datetime) -> list[dict]:
    bbox = ",".join(str(x) for x in BBOX)
    params = {
        "bbox": bbox,
        "time": f"{day_start.strftime('%Y-%m-%dT%H:%M:%SZ')},{day_end.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    }
    url = f"{OSM_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            xml_bytes = resp.read()
    except Exception as e:
        print(f"   ⚠️  fetch failed for {day_start.date()}: {e}")
        return []

    root = ET.fromstring(xml_bytes)
    out: list[dict] = []
    for cs in root.findall("changeset"):
        out.append(_parse_changeset(cs))
    return out


def _parse_changeset(cs: ET.Element) -> dict:
    a = cs.attrib
    # Changeset tags: free-form k/v. The comment is the human description; the
    # rest (hashtags, source, etc.) become the activity tags.
    comment = ""
    tags: list[str] = []
    for tag in cs.findall("tag"):
        k, v = tag.get("k", ""), tag.get("v", "")
        if not v:
            continue
        if k == "comment":
            comment = v
        elif k == "hashtags":
            tags.extend(h.strip().lstrip("#") for h in v.replace(",", ";").split(";") if h.strip())
        elif k in ("source", "imagery_used"):
            tags.extend(s.strip() for s in v.split(";") if s.strip())

    def fnum(key):
        try:
            return float(a[key])
        except (KeyError, ValueError, TypeError):
            return None

    bbox = {
        "min_lon": fnum("min_lon"), "min_lat": fnum("min_lat"),
        "max_lon": fnum("max_lon"), "max_lat": fnum("max_lat"),
    }
    changeset_id = a.get("id", "")
    return {
        "name": changeset_id,
        "fields": {
            "changeset_id": changeset_id,
            "user_id":      a.get("user") or a.get("uid") or "anonymous",
            "created_at":   a.get("created_at", ""),
            "bbox":         bbox,
            "tags":         tags,
            "comment":      comment,
            "num_changes":  int(a.get("changes_count", a.get("num_changes", 0)) or 0),
        },
    }


def run():
    records: list[dict] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc)

    print(f"🌍 Pulling OSM changesets for bbox {BBOX} — target {TARGET_COUNT:,}")
    for d in range(DAYS_BACK):
        day_end = now - timedelta(days=d)
        day_start = day_end - timedelta(days=1)
        batch = _fetch_day(day_start, day_end)
        added = 0
        for rec in batch:
            cid = rec["name"]
            if not cid or cid in seen:
                continue
            # require a usable spatial anchor so the map renders
            if rec["fields"]["bbox"]["min_lon"] is None:
                continue
            seen.add(cid)
            records.append(rec)
            added += 1
        print(f"   📅 {day_start.date()} → {added} new ({len(records):,} total)")
        if len(records) >= TARGET_COUNT:
            break
        time.sleep(REQUEST_PAUSE_SEC)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Wrote {len(records):,} changesets → {OUTPUT_PATH}")
    if records:
        print("   sample fields:", list(records[0]["fields"].keys()))


if __name__ == "__main__":
    run()
