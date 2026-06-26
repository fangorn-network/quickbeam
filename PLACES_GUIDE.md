# Local-Business (Google Places) Pipeline

Turn a real, physical small business — e.g. **Shotski's in Eagle River, WI** — into
a tiny semantic-search shard you can demo on a phone. This is the local-SMB twin
of the MusicBrainz pipeline: a new *domain*, not a new *architecture*. The scraper
populates a raw store, then the **same** `schemagen → build → cdn` path that
serves music serves bars.

```
 ┌──────────────────────┐
 │  Google Places API   │  places-fetch → sweep an area, pull Place Details
 │      (new, v1)       │  (hours, contact, rating, categories, user reviews)
 └──────────┬───────────┘
            ▼
 ┌──────────────────────┐
 │  Postgres places_raw │  raw jsonb cache  (optional — see "Do we need Postgres?")
 │   …or a JSONL file   │
 └──────────┬───────────┘
            ▼  placespg  (raw payloads → typed graph)
 ┌──────────────────────┐
 │  volume_*.json nodes │  Business / Review / Category / Reviewer / Locality
 │  + volume_*_edges    │  hasReview / byAuthor / inCategory / locatedIn / near
 └──────────┬───────────┘
            ▼  schemagen  (existing utility — unchanged)
 ┌──────────────────────┐
 │  Fangorn schema +    │  fangorn.places.localcore.v1 bundle
 │  bundle definition   │
 └──────────┬───────────┘
            ▼  build  (--root-profile business)  →  cdn bake
 ┌──────────────────────┐
 │  <100KB demo shard   │  shotskis.sond3r.network — browser WASM semantic search
 └──────────────────────┘
```

---

## Prerequisites

- A Google Cloud key with **"Places API (new)"** enabled and billing on. Export it:
  ```bash
  export GOOGLE_PLACES_API_KEY=AIza...
  ```
- For the Postgres path (optional), a reachable database. The official `postgres`
  image creates a `postgres` superuser and a `postgres` database — **not** the
  `places`/`places_db` the default DSN expects — so create those once.

  ```bash
  # trust auth = no password needed.
  docker run -d --name places \
    -p 5433:5432 \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    postgres

  # create the role + database the DSN points at (password is incidental under trust)
  docker exec places psql -U postgres -c "CREATE ROLE places LOGIN PASSWORD 'places';"
  docker exec places psql -U postgres -c "CREATE DATABASE places_db OWNER places;"

  # view first 5 rows
  psql "postgresql://places:places@localhost:5432/places_db" -c "SELECT * FROM places_raw LIMIT 5;"
  # enter the postgres
  psql "postgresql://places:places@localhost:5432/places_db"  
  # view tables
  \dt
  

  export PLACES_PG_DSN=postgresql://places:places@localhost:5433/places_db
  ```

  The `places_raw` table is created automatically on first run.

  **Gotchas:**
  - "password authentication failed" usually means you reached a *host* Postgres
    on 5432, not the container. Check what's listening (`ss -ltnp | grep 5432`)
    and confirm the container actually published a port
    (`docker port places` should show `... -> 0.0.0.0:5433`).
  - This container has no volume, so `docker rm` drops the data. Add
    `-v places_data:/var/lib/postgresql/data` to persist it.
  - Don't want a database at all? Skip this entirely and use the no-database
    variant below (`places-fetch --no-db --raw-out` → `placespg --raw-in`).

---

## Stage A — scrape into the raw store

`quickbeam data places-fetch` sweeps an area, pulls full Place Details for every
hit, and stores the **verbatim** API payload. Mark the pitch target with
`--anchor` (a case-insensitive name substring) so the demo can centre on it.

```bash
# Text Search around the target, flagging Shotski's as the anchor
quickbeam data places-fetch \
  --query "bars, restaurants, and banks near Eagle River, WI" \
  --anchor "Shotskis" \
  --max-results 500 --dry-run
```

Martin lives in:
Hofhein, Germany
50.085145, 8.446613

Other ways to source place IDs:

```bash
# Nearby Search by coordinate + radius (metres) and category types
quickbeam data places-fetch --location 45.917,-89.244 --radius 2000 \
  --types "bar,restaurant,night_club" --anchor "Shotski" --dry-run

# Adaptive sweep (--sweep): beat Google's 20-result cap automatically.
# Start with one big circle; any tile that comes back FULL (20 hits) is
# recursively subdivided into four overlapping quarters until every tile
# returns < 20 — at which point the area is provably captured. Sparse areas
# stay one cheap call; dense downtowns auto-zoom. See "Sweeping…" below.
quickbeam data places-fetch --location 45.917,-89.244 --radius 10000 \
  --types "bar,restaurant,night_club" --sweep --dry-run

# Just one (or a few) known place IDs — skips search entirely
quickbeam data places-fetch --details-only "ChIJ...,ChIJ..."
```

Re-running is idempotent: rows upsert by `place_id`, `fetched_at` refreshes, and
the anchor flag is sticky (a later sweep can set it but won't clear it).

### Sweeping an area to completion (`--sweep`)

The new Nearby Search has **no pagination** (no `nextPageToken`) and a hard **20-
result cap** per call, ranked by Google's opaque "prominence". A single call over
a dense area silently drops everything past the top 20 — and gives you no signal
that you missed anything. The industry-standard fix is **recursive grid
subdivision (quadtree tiling)**: let Google's own response tell you when to dig
deeper.

`--sweep` implements it. Using `--location`/`--radius` as the root circle:

1. Search the circle. If it returns **fewer than 20** hits, you captured
   *everything* inside it — save and move on (1 cheap call).
2. If it returns **exactly 20**, you've hit the ceiling — there are almost
   certainly more. Subdivide into four overlapping sub-circles (NW/NE/SW/SE) at
   0.75× the radius (the geometric floor for fully tiling the parent is 0.707×;
   the extra margin avoids cardinal-edge gaps) and recurse.
3. Stop when a tile drops below the cap, or the radius would fall below
   `--min-radius` (the floor that prevents infinite zoom on one hyper-dense
   block).

```bash
# Sweep a 10km area to completion — auto-zooms only where the data is dense
quickbeam data places-fetch --location 45.917,-89.244 --radius 10000 \
  --types "bar,restaurant,night_club" --sweep \
  --min-radius 500 --max-tiles 200 --anchor "Shotski"
```

Each tile prints its radius, centre, hit count, and whether it subdivided, so you
can watch the sweep adapt. Sparse rural circles cost one call; a downtown strip
fans out automatically until every sub-circle clears.

**Cost note:** only the cheap **Essentials-tier Search** calls multiply during a
sweep — the expensive **Place Details** call still fires just *once per unique
business* (dedup is by `place_id`). `--max-tiles` caps total Search calls and
`--max-details` caps billable Details calls; use `--dry-run` first to see how many
of each a real run would make. The 20-cap, the floor, and the tile ceiling are all
reported as the sweep runs.

> **OSM alternative.** To avoid paying Google just to *find* venues, query
> OpenStreetMap's Overpass API (free) for `amenity=bar`/`restaurant` nodes, then
> feed those coordinates straight to `--details-only` — paying Google only for the
> rich Details payload of businesses you already know exist. The catch: OSM
> coverage is thin in many small localities, so `--sweep` remains the reliable
> path where OSM is sparse.

### No-database variant

Don't want Postgres? Append the raw payloads to a JSONL file instead:

```bash
quickbeam data places-fetch --no-db --raw-out shotskis_raw.jsonl \
  --query "bars near Eagle River, WI" --anchor "Shotski" --dry-run
```

(`--raw-out` also works *alongside* Postgres if you want both.)

---

## Stage B — raw store → Fangorn graph

`quickbeam data placespg` shapes the raw payloads into the node/edge volume files
the rest of the pipeline consumes. The shaping is identical whichever source you
use:

```bash
# from Postgres (default)
quickbeam data placespg --output-dir ./stage_volumes

# from the JSONL file (no database)
quickbeam data placespg --raw-in shotskis_raw.jsonl --output-dir ./stage_volumes
```

`--near-radius-m` (default 1500) controls the `near` edges drawn between
businesses by great-circle distance; set `0` to turn them off.

> Multiple raw sources (OSM, Wikidata, …) can later feed this same schema via a
> dedup-aware synthesizer — see `OSM_ENHANCE_INVESTIGATE.md`. (A Yelp source was
> prototyped but removed: Yelp retired its free Fusion tier.)

**Output** (`./stage_volumes/`):

| File | Node type | Key fields |
|------|-----------|-----------|
| `volume_1_businesses.json` | `Business` | title, address, coordinates, hours, rating, priceLevel, amenities, editorialSummary, **isAnchor**, `text` |
| `volume_1_reviews.json` | `Review` | author, rating, body, relativeTime, `text` |
| `volume_1_categories.json` | `Category` | title, rawType |
| `volume_1_reviewers.json` | `Reviewer` | title (author name), profileUri |
| `volume_1_localities.json` | `Locality` | title, region |
| `volume_1_edges.json` | — | `hasReview`, `byAuthor`, `inCategory`, `locatedIn`, `near` |

Every node carries a verbalized `text` field — that's what gets embedded. For a
`Business`, `text` folds name, type, locality, editorial summary, rating, price,
amenities, and hours into one sentence; review bodies are the high-value
contextual signal and live on their own `Review` nodes, joined back via
`hasReview`.

---

## Stage C — schema, embeddings, shard (existing utilities, unchanged)

```bash
# 1) infer schemas + bundle from the volume files
quickbeam data schemagen \
  --input-dir ./stage_volumes \
  --prefix fangorn.places --bundle-name localcore --version v1

# 2) register + publish the bundle via the Fangorn SDK (node schemas first, then
#    the bundle), which yields a bundle schema id 0x...

# 3) build embeddings, projecting one document per Business
quickbeam build --bundle fangorn.places.localcore.v1=0x<bundleId> \
  --root-profile business

# 4) bake the immutable, pullable demo shard
quickbeam cdn bake ...
```

The **`business` root profile** (in `embeddings.py`'s `ROOT_PROFILES`) walks each
`Business` out to depth 2 and folds in its reviews, categories, locality,
reviewers, and nearby businesses — exactly the projection the per-bar demo shard
needs. You can override or extend it via `--profiles-file`.

---

## Cost & staying inside the free tier

Only **Stage A (`places-fetch`)** costs money. `placespg`, `schemagen`, and
`build` all read the cache for free — so once a business is scraped, you can
re-process it endlessly at no cost.

Billing is **per SKU, set by the field mask** (you pay the tier of the most
expensive field you ask for). This pipeline's masks:

| Call | Field mask | SKU tier | When |
|------|-----------|----------|------|
| Text / Nearby Search | `id,nextPageToken` (IDs only) | Essentials (cheap) | 1–3 per sweep |
| Place Details | full mask incl. `reviews` + atmosphere | **Enterprise + Atmosphere** (priciest) | **1 per business** |

A 60-business sweep ≈ 60 Atmosphere-tier Details calls + a couple of Search
calls. Small, but reviews make each Details call the most expensive kind there is.

**Hard guardrails (do these once in Google Cloud — only these actually stop spend):**

1. **Quota cap** — APIs & Services → *Places API (new)* → **Quotas** → set
   "Requests per day" to a ceiling. Over-limit calls are *rejected*, not billed.
2. **Budget alerts** — Billing → **Budgets & alerts** (notify only; don't cut off).
3. **Metrics** — APIs & Services → **Metrics**, grouped by SKU, to watch usage vs.
   the monthly free allotment.

**Built-in guards in `places-fetch`:**

```bash
# preview cost: runs only the cheap Search, reports how many billable Details
# calls a real run WOULD make — fetches nothing, stores nothing
quickbeam data places-fetch --query "bars near Eagle River, WI" --dry-run

# hard ceiling on billable Place Details calls per run (default 60)
quickbeam data places-fetch --query "..." --max-details 25
```

Every real run ends with a tally, e.g.
`💳 Billable this run: 2 Search + 23 Place Details (Enterprise+Atmosphere)`.
Because the raw store dedups by `place_id`, re-sweeping an area only pays for
businesses you haven't already cached.

> Exact prices and the size of the monthly free allotment change periodically —
> check the current Google Maps Platform pricing page and your Cloud Console
> Metrics rather than trusting a number hard-coded here.

---

## Do we need Postgres?

**No — it's optional.** Two myths worth dispelling:

- **Schema building does not read Postgres.** `schemagen` infers schemas from the
  `volume_*.json` files, not the database. The graph builder (`placespg`) can read
  those payloads straight from a JSONL file (`--raw-in`), so the whole chain runs
  database-free.
- **Postgres is not the ingest target.** The real destinations are **Fangorn** (the
  published bundle) and **Qdrant** (the embeddings). `places_raw` is only a *raw
  cache* sitting in front of them.

So why keep it? Because for the **demo-factory** use case it pays for itself:

1. **Decouples the paid API from reprocessing.** You scrape once (costs money +
   burns quota), then iterate on the node/edge model and re-run `placespg →
   schemagen → build` as many times as you like without re-hitting Google.
2. **Idempotent, incremental refresh.** Upsert by `place_id`, sticky anchors,
   `fetched_at` for staleness — trivial to wire a nightly "refresh the corpus"
   job.
3. **Accumulation across sweeps.** Many bars, many areas, over time, all land in
   one queryable table — the substrate for auto-provisioning many demo links.
4. **Debuggable.** When shaping misbehaves, inspect the raw jsonb with plain SQL.

**Rule of thumb:** one-off demo for a single bar → use `--no-db --raw-out` and
skip it. Standing up a repeatable factory across many businesses → keep Postgres.

---

## A note on Google's terms

Google's Places API restricts long-term caching of Places content — review text in
particular. Treat `places_raw` (or the JSONL) as a **short-lived prototype cache**,
not a permanent warehouse, and don't redistribute raw review text.

**OpenStreetMap** (see `quickbeam data osm`) is the ToS-clean alternative for
POIs, hours, contact info, and categories — it has no reviews/ratings, but its
output can feed the *same* `Business`/`Category`/`Locality` schema. The node/edge
model here is deliberately source-agnostic so OSM can fill these types later
without touching the schema or the build path.
