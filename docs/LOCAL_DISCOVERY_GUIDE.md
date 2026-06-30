# Local Discovery Pipeline — Places + Events

Turn a real locality — its **physical businesses** (Google Places: hours, ratings,
reviews) **and the events happening at them** (Eventbrite + The Events Calendar) —
into one tiny semantic-search shard you can demo on a phone. Opening *Shotskis the
bar* shows its events; a search like *"live music this summer"* returns events
ranked alongside places; a search for *"tacos"* surfaces the business a reviewer
raved about.

This is two **domains** feeding one **architecture**. Each source populates a raw
store, a shaper emits typed nodes/edges into a shared `stage_volumes/`, and the
**same** `schemagen → build → cdn` path serves all of it. Places are
`volume_1_*`; events are `volume_2_*`, linked back to their venue `Business` by
coordinate match.

```
 ┌──────────────────────┐   ┌────────────────────────┐   ┌────────────────────────┐
 │  Google Places API   │   │ Eventbrite organizer   │   │ The Events Calendar     │
 │      (new, v1)       │   │ /location / showmore    │   │ (Tribe) /wp-json/tribe  │
 │  places-fetch        │   │ events-fetch            │   │ events-fetch            │
 └──────────┬───────────┘   └───────────┬────────────┘   └───────────┬────────────┘
            ▼                            ▼                            ▼
 ┌──────────────────────┐    ┌──────────────────────────────────────────────────┐
 │  Postgres places_raw │    │  Postgres events_raw                              │
 │   …or a JSONL file   │    │   …or a JSONL file                                │
 └──────────┬───────────┘    └──────────────────────┬───────────────────────────┘
            ▼ placespg                               ▼ eventspg  (MERGED w/ places)
 ┌──────────────────────┐    ┌──────────────────────────────────────────────────┐
 │  volume_1_* nodes    │    │  volume_2_events / _organizers                    │
 │  Business / Review / │    │  Event / Organizer (+ reuse Category / Locality)  │
 │  Category / Reviewer │    │  hostedAt  Event → Business  ◄── coordinate match │
 │  / Locality / edges  │    │  hostsEvent Business → Event                      │
 └──────────┬───────────┘    └──────────────────────┬───────────────────────────┘
            └──────────────┬─────────────────────────┘
                           ▼  schemagen --volume 0  →  build / prebake  →  cdn bake
                ┌──────────────────────────────────────┐
                │  one <100KB shard: Businesses +       │  browser WASM
                │  Reviews + Events                     │  semantic search
                └──────────────────────────────────────┘
```

Only **Google Places (Stage A)** costs money. Everything downstream reprocesses
the cache for free. The event scrapers are **free and need no API key**; we store
event/place *metadata* (and short-lived review text) only — see "Terms of service".

---

## Prerequisites

### Google Places key

A Google Cloud key with **"Places API (new)"** enabled and billing on:

```bash
export GOOGLE_PLACES_API_KEY=AIza...
```

### Postgres (optional, shared by both pipelines)

Postgres is a *raw cache* in front of the real targets (Fangorn + Qdrant), not
the ingest target. It's optional — see "Do we need Postgres?" — but recommended
for a repeatable factory. The official `postgres` image creates a `postgres`
superuser and database, **not** the `places`/`places_db` the default DSN expects,
so create those once:

```bash
# trust auth = no password needed
docker run -d --name places \
  -p 5432:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres

# create the role + database the DSN points at (password is incidental under trust)
docker exec places psql -U postgres -c "CREATE ROLE places LOGIN PASSWORD 'places';"
docker exec places psql -U postgres -c "CREATE DATABASE places_db OWNER places;"

export PLACES_PG_DSN=postgresql://places:places@localhost:5432/places_db
# events reuse the SAME database — events_raw lands next to places_raw
export EVENTS_PG_DSN=postgresql://places:places@localhost:5432/places_db
```

Both `places_raw` and `events_raw` tables auto-create on first run (inside an
existing database — Postgres can't auto-create the *database* itself).

```bash
psql "postgresql://places:places@localhost:5432/places_db"   # connect
\dt                                                          # list tables
```

**Gotchas:**
- "password authentication failed" / **old data after a `docker prune`** usually
  means you reached a *host* Postgres on 5432, not the container — `docker
  system prune` can't touch a host-installed service. Check what's listening
  (`ss -ltnp | grep 5432`) and that the container published its port
  (`docker port places`). To make the container own 5432:
  ```bash
  sudo systemctl disable --now postgresql   # stop the host PG
  docker restart places                     # let the container rebind loopback
  ```
- A dedicated events database instead of sharing needs a **superuser** (the
  `places` login role can't `CREATE DATABASE`):
  ```bash
  psql "postgresql://postgres@localhost:5432/postgres" \
    -c "CREATE ROLE events LOGIN PASSWORD 'events';" \
    -c "CREATE DATABASE events_db OWNER events;"
  export EVENTS_PG_DSN="postgresql://events:events@localhost:5432/events_db"
  ```
- This container has no volume, so `docker rm` drops the data. Add
  `-v places_data:/var/lib/postgresql/data` to persist it.
- Don't want a database at all? Use the no-DB variant of each scraper
  (`--no-db --raw-out file.jsonl` → shaper `--raw-in file.jsonl`).

---

## Stage A — scrape into the raw store

### A1. Places (`places-fetch`)

Sweeps an area, pulls full Place Details for every hit, and stores the
**verbatim** API payload. Mark the pitch target with `--anchor` (a
case-insensitive name substring) so the demo can centre on it.

```bash
# Text Search around the target, flagging Shotski's as the anchor
quickbeam data places-fetch \
  --query "bars, restaurants, and banks near Eagle River, WI" \
  --anchor "Shotskis" \
  --max-results 500 \
  --dry-run
```

Other ways to source place IDs:

```bash
# Nearby Search by coordinate + radius (metres) and category types — 20-result cap
quickbeam data places-fetch --location 45.917,-89.244 --radius 2000 \
  --types "bar,restaurant,night_club" --anchor "Shotski" --dry-run

# Adaptive sweep (--sweep): beat Google's 20-result cap automatically (see below)
quickbeam data places-fetch --location 45.917,-89.244 --radius 10000 \
  --types "bar,restaurant,night_club" --sweep --dry-run

# Just one (or a few) known place IDs — skips search entirely
quickbeam data places-fetch --details-only "ChIJ...,ChIJ..."
```

Re-running is idempotent: rows upsert by `place_id`, `fetched_at` refreshes, and
the anchor flag is sticky (a later sweep can set it but won't clear it).

**No database?** Append the raw payloads to a JSONL file:

```bash
quickbeam data places-fetch --no-db --raw-out shotskis_raw.jsonl \
  --query "bars near Eagle River, WI" --anchor "Shotski" --dry-run
```

(`--raw-out` also works *alongside* Postgres if you want both.)

#### Sweeping an area to completion (`--sweep`)

The new Nearby Search has **no pagination** and a hard **20-result cap** per call,
ranked by Google's opaque "prominence". A single call over a dense area silently
drops everything past the top 20 with no signal you missed anything. The fix is
**recursive grid subdivision (quadtree tiling)** — let Google's own response tell
you when to dig deeper. Using `--location`/`--radius` as the root circle:

1. Search the circle. If it returns **fewer than 20** hits, you captured
   *everything* inside it — save and move on (1 cheap call).
2. If it returns **exactly 20**, you've hit the ceiling — subdivide into four
   overlapping sub-circles (NW/NE/SW/SE) at **0.75×** the radius (the geometric
   floor for fully tiling the parent is 0.707×; the extra margin avoids
   cardinal-edge gaps) and recurse.
3. Stop when a tile drops below the cap, or the radius would fall below
   `--min-radius` (the floor that prevents infinite zoom on one hyper-dense block).

```bash
# Sweep a 10km area to completion — auto-zooms only where the data is dense
quickbeam data places-fetch --location 45.917,-89.244 --radius 10000 \
  --types "bar,restaurant,night_club" \
  --sweep --min-radius 500 --max-tiles 200 \
  --anchor "Shotskis" --dry-run

# Hofheim, DE, 10km radius, broad "everyday life" net
quickbeam data places-fetch --location 50.085145,8.446613 --radius 10000 \
  --types "store,restaurant,bar,cafe,bakery,..." \
  --sweep --min-radius 500 --max-tiles 200 --dry-run
```

Each tile prints its radius, centre, hit count, and whether it subdivided, so you
can watch the sweep adapt. Sparse rural circles cost one call; a downtown strip
fans out automatically until every sub-circle clears.

**Cost note:** only the cheap **Essentials-tier Search** calls multiply during a
sweep — the expensive **Place Details** call still fires just *once per unique
business* (dedup by `place_id`). `--max-tiles` caps total Search calls and
`--max-details` caps billable Details calls; use `--dry-run` first.

> **OSM alternative.** To avoid paying Google just to *find* venues, query
> OpenStreetMap's Overpass API (free) for `amenity=bar`/`restaurant` nodes, then
> feed those coordinates to `--details-only` — paying Google only for the rich
> Details payload of businesses you already know exist. The catch: OSM coverage is
> thin in many small localities, so `--sweep` remains the reliable path where OSM
> is sparse.

#### Place-type nets

quickbeam data places-fetch --location 45.917,-89.244 --radius 10000 \
  --types "campground,marina,park,tourist_attraction,resort_hotel,lodging,bed_and_breakfast,sporting_goods_store,amusement_center,museum,gift_shop,hardware_store,bar,restaurant,american_restaurant,pub,bar_and_grill,brewery,pizza_restaurant,cafe,coffee_shop,bakery,ice_cream_shop,fast_food_restaurant,sandwich_shop,grocery_store,supermarket,convenience_store,liquor_store,clothing_store,home_goods_store,gas_station,pharmacy,store,auto_parts_store,car_repair,bank,atm,hair_care,beauty_salon,fitness_center,medical_clinic,dentist,doctor,veterinary_care,real_estate_agency,laundry" \
  --sweep --min-radius 500 --max-tiles 200 \
  --anchor "Shotskis" --dry-run


Ready-made `--types` lists for common sweeps:

*Every Day Life & Commerce*
```
store,restaurant,bar,cafe,bakery,grocery_store,supermarket,convenience_store,clothing_store,electronics_store,home_goods_store,shopping_mall,pharmacy,drugstore,bank,atm,gas_station,car_repair,hair_care,beauty_salon,spa,laundry,post_office,library,community_center,fitness_center,gym,medical_clinic,dentist,doctor,veterinary_care,real_estate_agency,hotel,lodging,bed_and_breakfast,hardware_store,liquor_store,pet_store,book_store,florist,department_store,fast_food_restaurant,sandwich_shop,coffee_shop,pizza_restaurant,barber_shop,auto_parts_store,furniture_store,wholesaler
```

*Leisure, Nightlife & Tourism*
```
restaurant,bar,night_club,pub,bar_and_grill,beer_garden,cocktail_bar,sports_bar,irish_pub,wine_bar,brewery,brewpub,cafe,coffee_shop,bistro,diner,fast_food_restaurant,ice_cream_shop,bakery,event_venue,live_music_venue,performing_arts_theater,movie_theater,comedy_club,bowling_alley,casino,amusement_center,tourist_attraction,museum,art_gallery,historical_place,cultural_center,park,city_park,plaza,garden,botanical_garden,winery,vineyard,resort_hotel,hotel,motel,aquarium,zoo,stadium,arena,sports_club,banquet_hall
```

*Transit & Infrastructure*
```
transit_station,transit_stop,bus_station,bus_stop,train_station,subway_station,light_rail_station,airport,international_airport,ferry_terminal,ferry_service,taxi_stand,taxi_service,parking,parking_garage,parking_lot,rest_stop,truck_stop,gas_station,electric_vehicle_charging_station,ebike_charging_station,car_rental,bridge,toll_station,transportation_service
```

### A2. Events (`events-fetch`)

Three sources behind one CLI, writing to `events_raw` (uses `EVENTS_PG_DSN` or
`--dsn`). All free, no API key.

```bash
# Eventbrite organizer (slug or URL). Upcoming events from the page's
# __NEXT_DATA__; past events + pagination from the internal show-more JSON API.
quickbeam data events-fetch --source eventbrite \
  --organizer shotskis-29817730199

# Eventbrite LOCATION discovery — find every public event across an area at once,
# without knowing any organizer ids. Each event carries its venue + coordinates,
# so the downstream coordinate match attaches it to whichever bar it happens at.
quickbeam data events-fetch --source eventbrite-location \
  --place wi--eagle-river

# add --expand-past to also pull each discovered organizer's past events.

# The Events Calendar (Tribe) site — the public WP REST API, paginated.
quickbeam data events-fetch --source tribe \
  --site https://eagleriver.org --max-events 500
```

**Non-US events (`--bbox`).** Eventbrite's geography is actually driven by a
`bbox` query param — `"west_lng,south_lat,east_lng,north_lat"` — not the
`/d/<slug>/` path. Pass a bbox to scrape any region regardless of the slug (which
defaults to `united-states` and is harmless once a bbox is set):

```bash
# Honheim / Stuttgart region, DE
quickbeam data events-fetch --source eventbrite-location \
  --bbox "8.817042,48.415394,9.617042,49.015394" --max-events 500
```

Shrink the box to tighten the target (a wide box spans neighbouring towns).

Re-running is idempotent — rows upsert by `event_key`. Eventbrite past events omit
the venue but carry `venue_id`; the fetcher backfills coordinates from the
organizer's other events. `--dry-run` reports counts without storing; `--no-db
--raw-out events.jsonl` skips Postgres.

> **Do I have to scrape each bar by hand?** No. `eventbrite-location` (or `--bbox`)
> returns every public event in the region with venue coordinates, and `eventspg`'s
> coordinate match links each to the matching `Business`. Bars with no Eventbrite
> presence simply get no events — that *is* the "which bars have events?" answer.
> Per-organizer `--source eventbrite` remains the way to pull one venue's *full*
> history.

---

## Stage B — raw store → Fangorn graph (one merged `stage_volumes/`)

### B1. Places → `volume_1_*` (`placespg`)

```bash
# from Postgres (default)
quickbeam data placespg --output-dir ./stage_volumes
# from the JSONL file (no database)
quickbeam data placespg --raw-in shotskis_raw.jsonl --output-dir ./stage_volumes
```

`--near-radius-m` (default 1500) controls the `near` edges drawn between
businesses by great-circle distance; set `0` to turn them off.

| File | Node type | Key fields |
|------|-----------|-----------|
| `volume_1_businesses.json` | `Business` | title, address, coordinates, hours, rating, priceLevel, amenities, editorialSummary, **isAnchor**, `text` |
| `volume_1_reviews.json` | `Review` | author, rating, body, relativeTime, `text` |
| `volume_1_categories.json` | `Category` | title, rawType |
| `volume_1_reviewers.json` | `Reviewer` | title (author name), profileUri |
| `volume_1_localities.json` | `Locality` | title, region |
| `volume_1_edges.json` | — | `hasReview`, `byAuthor`, `inCategory`, `locatedIn`, `near` |

### B2. Events → `volume_2_*`, merged with places (`eventspg`)

```bash
quickbeam data eventspg --output-dir ./stage_volumes
# (or from JSONL: quickbeam data eventspg --raw-in events.jsonl --output-dir ./stage_volumes)
```

| File | Node type | Key fields |
|------|-----------|-----------|
| `volume_2_events.json` | `Event` | title, dateLabel, startDate, venueName, address, coordinates, locality, priceLevel, ticketUrl, organizerName, isPast, isCancelled, categories, source, `text` |
| `volume_2_organizers.json` | `Organizer` | title, bio, website, facebook, source |

The **`hostedAt` / `hostsEvent`** edges link each Event to the nearest existing
`Business` within `--match-radius-m` (default 120 m; Shotskis' Eventbrite venue is
~12 m from its Google Place), falling back to a venue-name / business-title match.
The Business index is read from `<output-dir>/volume_1_businesses.json` (override
with `--businesses-in`); events with no match still stand alone.

**Every node carries a verbalized `text` field — that's what gets embedded.** A
`Business` folds name, type, locality, editorial summary, rating, price, and
amenities into one sentence; review bodies are the high-value contextual signal and
live on their own `Review` nodes, joined back via `hasReview`. This child→parent
structure is what lets a review-text search ("tacos", "runny cheese sauce") surface
the *business*, not a bare review.

> Multiple raw sources (OSM, Wikidata, …) can later feed this same schema via a
> dedup-aware synthesizer. The node/edge model is deliberately source-agnostic so a
> new source only needs a `normalize_*` function returning the common dict — the
> shaping, merge link, schema, and UI are unchanged.

---

## Stage C — schema, embeddings, shard

```bash
# 1) infer schemas across BOTH volumes (one bundle covering places + events).
#    --volume 0 = "all volumes": merges node types shared across volumes (e.g.
#    Category) and combines every edges file, so the bundle gains the Event /
#    Organizer schemas and the hostedBy / hostedAt / hostsEvent edge shapes.
quickbeam data schemagen --input-dir ./stage_volumes --volume 0 \
  --prefix fangorn.places --bundle-name eagleriver-localcore --version v2

# 2) register + publish to Fangorn with the SDK's publish script (in the fangorn
#    repo, NOT quickbeam). It reads <input-dir>/schemas/fangorn_schemas.json,
#    registers every node schema (carrying its `identity`) + the bundle, then
#    commits the whole graph as ONE tx and prints the bundle id 0x… + the exact
#    `quickbeam build` line to run next.
cd ~/fangorn/fangorn
pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir ~/fangorn/embeddings/stage_volumes --volume 0
# → "registered bundle …localcore.v1 → 0x<bundleId>"  (use this id below)
```

> **publish_bundle.ts env.** The script needs a funded key + a Pinata pin/gateway,
> from either `~/.fangorn/config.json` or env: `DELEGATOR_ETH_PRIVATE_KEY`,
> `PINATA_JWT`, `PINATA_GATEWAY`, `CHAIN_NAME` (optional `RPC_URL`). Registration is
> idempotent — re-running skips already-registered schemas and re-publishes the
> data. A big graph? `--shard-roots <n>` switches to laptop-RAM-bounded sharded
> publishing (one tx per shard, resumable via a ledger).

Then embed + bake. Two paths:

**Production (on-chain):**

```bash
quickbeam build --bundle fangorn.places.localcore.v1=0x<bundleId> \
  --root-profile business --root-profile review --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

The root profiles (in `embeddings.py`'s `ROOT_PROFILES`):
- **`business`** — walks each Business out to depth 2, folding in its reviews,
  categories, locality, reviewers, and events.
- **`review`** — one document per Review (folding its Business + Reviewer), so
  review text is a first-class retrieval key that rolls up to its business.
- **`localevent`** — one document per Event, folding in its venue Business,
  organizer, category, locality.

Override or extend via `--profiles-file`.

> **One bundle vs. a Composed View.** This guide fuses places + events into **one
> bundle** (`schemagen --volume 0`), joined by the coordinate match `eventspg`
> bakes in — a single publisher, a single datasource. That is the right tool when
> you own both domains. When the domains are published as **separate datasources**
> (especially by *different* publishers), join them with a Phase-1 **Composed
> View** instead: it fuses on global identity rather than co-location. `schemagen`
> already emits each node type's `identity` (the `@id` + namespaced aliases like
> `gplace:` for a Place ID, `isrc:` for a recording) into `fangorn_schemas.json`,
> and `publish_bundle.ts` registers it — so every published node carries an Entity
> URI + aliases. Register a `view` over the source datasources, then build it with:
>
> ```bash
> quickbeam build --view fangorn.places.creativeview.v1=0x<viewId> \
>   --root-profile business --root-profile localevent --reset
> ```
>
> The view resolves its sources, fetches all of them into one graph, and
> **union-find merges nodes that share an alias** (e.g. a Business and an Event
> venue with the same `gplace:` Place ID) before projecting — deterministic, no
> linkset, no ML. See `docs/CROSS_PUBLISHER_LINKING_PLAN.md` (fangorn) §4.
>
> Publishing each domain as its own datasource this way is also what lets you
> refresh *only* events later, or let a claimed business own and update its own
> profile record — covered in **Stage E**.

**Local demo (no chain) — `prebake`:** embed local volume node files straight into
Qdrant with the exact same `nomic-embed-text-v1.5` + matryoshka-256 recipe `build`
uses, so vectors are comparable to build-time and to in-browser query vectors:

```bash
quickbeam data prebake --input-dir ./stage_volumes --volume 2 \
  --types Event,Organizer --collection fangorn --link-events
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

`--link-events` folds each bar's hosted-event titles into its payload
(payload-only — existing bar vectors untouched).

### Quickstart — rebuild the places shard end to end

```bash
quickbeam data placespg --output-dir ./stage_volumes
quickbeam data eventspg --output-dir ./stage_volumes
quickbeam data schemagen --input-dir ./stage_volumes --volume 0 \
  --prefix fangorn.places --bundle-name localcore --version v1
# republish the bundle → new 0x<bundleId>
quickbeam build --bundle fangorn.places.localcore.v1=0x<bundleId> \
  --root-profile business --root-profile review --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

---

## Stage D — the demo

`domains.json` (and the qdrant-mode overlay `examples/public/domain.json`) put
`Event` in the places domain filter and add presentation for `Event`/`Organizer`.
The examples app renders an event-flavored entity page (Upcoming/Past badge, date,
venue with a `◎ Nearby` coordinate-proximity link, price, a **Tickets ↗** link,
and the organizer); a bar lists its events under Connections (via the folded
`events` field); and the results rail gains an **"Upcoming events only"** toggle
that drops past events while leaving places unaffected.

```bash
cd examples && npm run build      # tsc + vite
```

### Configure language + locality (per-community deploy)

One examples build serves **one community in one language**. The data is
locality-agnostic (any Places/Events sweep works), but the UI chrome — hero copy,
search placeholder, the culturally-grounded "vibe" quick-searches, event labels —
comes from a **locale profile**, so a German locality doesn't render Northwoods
English. Profiles live in `examples/src/lib/i18n/`; each bundles `lang` (BCP-47) +
`community` (name/region/tagline) + `strings` (the full typed copy contract) +
`vibes`. Two ship today:

| `VITE_LOCALE` | Language · community | Vibes are grounded in |
|---------------|----------------------|-----------------------|
| `en-eagle-river` (default) | English · Eagle River, WI | supper clubs, Friday fish fry, lakeside |
| `de-hofheim` | Deutsch · Hofheim am Taunus, HE | Apfelwein/Äppler, Biergarten, Weinstube |

```bash
# Build the German deployment (sets <html lang>, title, all copy + vibes)
cd examples && VITE_LOCALE=de-hofheim npm run build
```

`VITE_LOCALE` also reads from `examples/.env.production`. For a quick locality
tweak **without** authoring a whole profile, override individual community fields:
`VITE_COMMUNITY_NAME`, `VITE_COMMUNITY_REGION`, `VITE_COMMUNITY_REGION_ABBR`,
`VITE_COMMUNITY_SLUG`, `VITE_COMMUNITY_TAGLINE`, `VITE_COMMUNITY_BLURB` (these layer
on top of the selected profile).

**Add a new community/language:** drop a `LocaleProfile` file in
`src/lib/i18n/` (copy `de-hofheim.ts` as a template — TypeScript's `Strings`
interface forces you to translate every key) and register it in
`src/lib/i18n/index.ts`. The `vibes` array is where the cultural grounding lives:
phrase the quick-searches the way locals actually search (regional drinks, venue
types, local rituals), since each `q` is folded into the semantic query.

> Locality vs. language are independent. The Hofheim Places data sits in Hessen
> (HE) while the earlier Eventbrite `--bbox` sweep pulled Stuttgart (BW) events —
> if you deploy one community, scrape both Places and Events for the *same* region
> (re-run the event `--bbox` around your locality) so the merged shard is coherent.

### Verify

```bash
# the merge link exists
grep -c '"rel": "hostedAt"' stage_volumes/volume_2_edges.json     # > 0
# events are searchable: "live music this summer" → LOLA Live, Marina Bar, …
# review text surfaces its business: "tacos" / "runny cheese sauce" → the bar
# upcoming filter keeps places, drops past events (Qdrant must_not on fields.isPast)
```

Re-running any stage is idempotent (upsert by id / `event_key`), and only Stage A
touches the network — every downstream stage reprocesses the cache for free.

---

## Stage E — Incremental updates & claimed business profiles

Stages A–D describe the **bulk publish**: one big sweep of OSM places + Eventbrite
events, baked into one shard. The interesting life of a locality, though, is what
happens *after*: a new month of events appears, and the bars themselves want to
correct their own hours and blurb. Both are **incremental, single-source
republishes** — and both rely on the **Composed View**, not the single merged
bundle, because a View fuses *independent datasources* and always resolves each one
to its **latest** commit.

> **Why the View, not the merged bundle.** `schemagen --volume 0` welds places +
> events into **one** datasource (one `resourceId`). To re-publish *only* events you
> would have to re-publish that whole combined datasource. Publish each domain as
> its **own** datasource instead — `schemagen --volume 3` (OSM places) and
> `--volume 2` (events) yield two bundles, two `resourceId`s — and join them with a
> `view`. On chain, `publish(manifestCid, root, schemaId, name)` writes a new
> manifest **version at a stable `resourceId`** (`keccak(owner, schemaId, name)`);
> the View resolver picks the highest-block manifest per source, so a fresh commit
> to one source is picked up on the next `build --view` while every other source
> stays pinned to its existing version.

### E0. Publish places and events as separate datasources

Do this once so the incremental updates have somewhere to land. (If you already published
a `--volume 0` merged bundle for the Stage A–D demo, this is the parallel "View"
layout — keep the merged bundle for the single-owner demo, or migrate to these two.)

> **Give each layout its own schema namespace.** A node schema name is
> `<prefix>.<type>.<version>` and is **immutable once registered**. The merged demo
> (Stage C) registers `fangorn.places.business.v1` from the *Google* Place shape;
> the OSM bundle below is a *different* `business` shape. Publishing both under the
> same `--prefix` collides — `publish_bundle.ts` fails fast with a shape-drift error.
> So the OSM places live under `fangorn.places.osm` and events under
> `fangorn.places.evt`. (Run only this layout? A single prefix is fine.)

```bash
# OSM places → its own bundle under the `…osm` namespace. schemagen (quickbeam) then publish (fangorn repo).
quickbeam data schemagen --input-dir ./stage_volumes --volume 3 \
  --prefix fangorn.places.osm --bundle-name placecore --version v1
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir ~/fangorn/embeddings/stage_volumes --volume 3
# → bundle id 0x<placesBundle>; its datasource has a stable resourceId R_places

# Eventbrite events → its own bundle under the `…evt` namespace.
quickbeam data schemagen --input-dir ./stage_volumes --volume 2 \
  --prefix fangorn.places.evt --bundle-name eventcore --version v1
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir ~/fangorn/embeddings/stage_volumes --volume 2
# → bundle id 0x<eventsBundle>; stable resourceId R_events
```

`publish_bundle.ts` publishes one bundle per `--volume`, so each domain is its own
datasource with its own `resourceId`. Register + publish the view that fuses them
with the companion script, **`publish_view.ts`** — it resolves each same-owner
`--source-bundle` name to its `resourceId` (using the same default datasetName
`publish_bundle.ts` commits under), registers the `view`, publishes its manifest,
and prints the view id + the `quickbeam build --view` line:

```bash
cd ~/fangorn/fangorn
pnpm dotenvx run -f .env -- tsx src/test/publish_view.ts \
  --name fangorn.places.localview.v1 \
  --source-bundle fangorn.places.osm.placecore.v1 \
  --source-bundle fangorn.places.evt.eventcore.v1
# → "registered view …localview.v1 → 0x<viewId>"  then  "✅ View published."
```

For a source published by **another** wallet, pass its `resourceId` directly with
`--source-resource 0x<rid>` (and `--linkset-name` / `--linkset-resource` to attach
asserted-edge linksets). Re-registration is idempotent by name — to *change* a
view's sources, bump the view name/version.

```bash
# build the fused shard from the view
quickbeam build --view fangorn.places.localview.v1=0x<viewId> \
  --root-profile business --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

Both bundles still carry `identity` from `schemagen`, so an `Event` whose
`hostBusinessId` is a Google Place ID (`gplace:` alias) fuses onto the matching OSM
`Business` *across the datasource boundary* — the same join the merged bundle got
from coordinate match, now from global identity.

### E1. Publish ONLY more eventbrite data later

A month passes; new events are live. Refresh **just the events source** — OSM places
are untouched, un-rescraped, and stay at their existing version:

```bash
# 1) scrape only the new events into the raw cache (idempotent upsert by event_key)
quickbeam data events-fetch --source eventbrite-location --place wi--eagle-river

# 2) re-shape ONLY events → stage_volumes/volume_2_* (places volume_3_* untouched)
quickbeam data eventspg --output-dir ./stage_volumes

# 3) re-run schemagen with the SAME `…evt` prefix — fangorn_schemas.json is
#    overwritten each run, so this restores the events schema the publish reads.
quickbeam data schemagen --input-dir ./stage_volumes --volume 2 \
  --prefix fangorn.places.evt --bundle-name eventcore --version v1

# 4) re-publish ONLY the events datasource — re-run publish_bundle.ts for --volume 2.
#    Same bundle name + same default datasetName ⇒ a NEW VERSION at the SAME
#    resourceId R_events. Schema registration no-ops; only data is re-published.
#    Do NOT touch --volume 3 (places stay at their existing version).
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir ~/fangorn/embeddings/stage_volumes --volume 2

# 5) rebuild the view shard — the resolver auto-selects the newest events manifest;
#    places resolve to their unchanged version. No view re-registration needed.
quickbeam build --view fangorn.places.localview.v1=0x<viewId> \
  --root-profile business --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

The `resourceId` is content-independent (`keccak(owner, schemaId, datasetName)`), so
re-running the **same** `--volume` (same bundle name, same default datasetName) is
what makes it a new *commit* rather than a new *source*. Keep that pair stable and
the View follows the chain forward on its own.

> A bundle re-publish replaces the **whole** manifest for that source (it is not a
> delta append). Because `events_raw` accumulates, step 2 re-shapes the *full*
> current event set — past + new — so the new manifest is complete. The win is
> **scope**, not diffing: you pay to publish one small domain, not the entire shard.

### E2. A claimed-business profile schema (self-sovereign, owner-updated)

So far every node is *scraped* — its authority is OSM/Google, and the publisher is
you. A **claimed business** wants to own its record: correct the hours, write the
real blurb, list today's specials — and have *that* outrank the scraped fields. The
clean way to model this is a **separate `BusinessProfile` datasource owned by the
business's own wallet**, fused onto the scraped `Business` by the shared Google
Place ID.

Register the profile schema with an `identity` that **shares the `gplace:`
namespace** — that namespace is the join contract, so the View's union-find collapses
the profile onto the same entity as the scraped `Business` (which already carries
`gplace:placeId`):

```ts
// the CLAIMED BUSINESS runs this from THEIR wallet (a distinct owner ⇒ distinct
// resourceId). The schema can be registered once and reused by every business.
await fangorn.schema.register({
  name: "fangorn.places.businessProfile.v1",
  definition: {
    placeId:      { "@type": "string" },   // the Google Place ID they are claiming
    officialName: { "@type": "string" },
    hours:        { "@type": "string" },
    description:  { "@type": "string" },
    menuUrl:      { "@type": "string" },
    updatedAt:    { "@type": "string" },
  },
  // promote the OWN id to @id; expose the SAME gplace namespace the scraped
  // Business uses, so identity fusion merges the two nodes.
  identity: { "@id": "placeId", aliases: { gplace: "placeId" } },
});

// publish ONE profile node — the business owns this datasource and re-publishes it
// whenever they edit (an E1-style single-source incremental update).
await fangorn.publisher.publishBundle({
  bundleName: "fangorn.places.businessProfile.v1",
  datasetName: "shotskis",                       // stable ⇒ edits are new versions
  nodes: [{
    id: "ChIJ....shotskis",                       // == placeId; becomes their Entity URI
    type: "BusinessProfile",
    fields: {
      placeId:      "ChIJ....shotskis",
      officialName: "Shotskis Bar & Grill",
      hours:        "Mon–Sun 11:00–02:00",
      description:  "Lakeside supper club & bar — live music Fridays.",
      menuUrl:      "https://shotskis.example/menu",
      updatedAt:    "2026-06-30",
    },
  }],
});
```

Add the profile datasource to the View's `sources` and rebuild. Because the profile
is published by the **business's** wallet, it is a foreign source to you — pass its
`resourceId` to `publish_view.ts` with `--source-resource`. A view's source set is
fixed at registration, so widening it means a **new view version** (`…localview.v2`):

```bash
cd ~/fangorn/fangorn
pnpm dotenvx run -f .env -- tsx src/test/publish_view.ts \
  --name fangorn.places.localview.v2 \
  --source-bundle fangorn.places.osm.placecore.v1 \
  --source-bundle fangorn.places.evt.eventcore.v1 \
  --source-resource 0x<profileResourceId>          # the claimed business's datasource
# → 0x<viewIdV2>
```

The View now fuses **three** members onto Shotskis' entity — the scraped OSM
`Business`, its `Event`s, and the owner's `BusinessProfile` — and the projected node
carries the union of their fields.

```bash
quickbeam build --view fangorn.places.localview.v2=0x<viewIdV2> \
  --root-profile business --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

**No Google Place ID to anchor on?** If a business has no scraped counterpart to
share a `gplace:` alias with (e.g. a brand-new venue OSM/Google hasn't indexed),
assert the equivalence explicitly with a **`sameAs` linkset** instead — a signed
`{ from: <profile Entity URI>, rel: "sameAs", to: <business Entity URI> }` edge feeds
the *same* union-find. See `checkpoint_guide_1.md` §3 and
`docs/CROSS_PUBLISHER_LINKING_PLAN.md` (fangorn) §5.

> **Whose field wins?** Today the View takes the union of members' fields; when a
> scraped field and a claimed field collide, prefer the claimed owner's value in your
> root profile's verbalizer (it is a distinct `owner`, so it is trivially
> identifiable). The principled version is the View's **`trust`** policy
> (`view.trust`, e.g. weight a verified-owner datasource above scraped ones), which
> the linking plan's trust phase formalizes — until then, owner-precedence in the
> profile is the pragmatic answer.

---

## Cost & staying inside the free tier

Only **Places Stage A (`places-fetch`)** costs money. Event scrapers are free;
`placespg`, `eventspg`, `schemagen`, `build`, `prebake`, and `bake` all read the
cache for free.

Google billing is **per SKU, set by the field mask** (you pay the tier of the most
expensive field you ask for). This pipeline's masks:

| Call | Field mask | SKU tier | When |
|------|-----------|----------|------|
| Text / Nearby Search | `id,nextPageToken` (IDs only) | Essentials (cheap) | 1–N per sweep |
| Place Details | full mask incl. `reviews` + atmosphere | **Enterprise + Atmosphere** (priciest) | **1 per business** |

A 60-business sweep ≈ 60 Atmosphere-tier Details calls + a couple of Search calls.

**Hard guardrails (do these once in Google Cloud — only these actually stop spend):**

1. **Quota cap** — APIs & Services → *Places API (new)* → **Quotas** → set
   "Requests per day". Over-limit calls are *rejected*, not billed.
2. **Budget alerts** — Billing → **Budgets & alerts** (notify only).
3. **Metrics** — APIs & Services → **Metrics**, grouped by SKU.

**Built-in guards in `places-fetch`:**

```bash
# preview cost: runs only the cheap Search, reports billable Details a real run
# WOULD make — fetches nothing, stores nothing
quickbeam data places-fetch --query "bars near Eagle River, WI" --dry-run

# hard ceiling on billable Place Details calls per run (default 60)
quickbeam data places-fetch --query "..." --max-details 25
```

Every real run ends with a tally, e.g.
`💳 Billable this run: 2 Search + 23 Place Details (Enterprise+Atmosphere)`.
Because the raw store dedups by `place_id`, re-sweeping an area only pays for
businesses you haven't already cached.

> Exact prices and the free allotment change periodically — check the current
> Google Maps Platform pricing page and your Cloud Console Metrics.

---

## Do we need Postgres?

**No — it's optional.** Two myths worth dispelling:

- **Schema building does not read Postgres.** `schemagen` infers schemas from the
  `volume_*.json` files; `placespg`/`eventspg` can read payloads straight from a
  JSONL file (`--raw-in`), so the whole chain runs database-free.
- **Postgres is not the ingest target.** The real destinations are **Fangorn** (the
  published bundle) and **Qdrant** (the embeddings). `places_raw`/`events_raw` are
  only a *raw cache* in front of them.

Why keep it for the **demo-factory** use case:

1. **Decouples the paid API from reprocessing.** Scrape once (costs money + quota),
   then iterate the node/edge model and re-run `*pg → schemagen → build` freely.
2. **Idempotent, incremental refresh.** Upsert by `place_id`/`event_key`, sticky
   anchors, `fetched_at` for staleness — trivial to wire a nightly refresh.
3. **Accumulation across sweeps.** Many businesses/events, many areas, one
   queryable table — the substrate for auto-provisioning many demo links.
4. **Debuggable.** Inspect the raw jsonb with plain SQL when shaping misbehaves.

**Rule of thumb:** one-off demo → `--no-db --raw-out`; a repeatable factory across
many businesses → keep Postgres.

---

## Terms of service

Google's Places API and Eventbrite both restrict long-term caching of their content
(review text in particular). Treat `places_raw` / `events_raw` (or the JSONL) as a
**short-lived prototype cache**, not a permanent warehouse; store metadata only and
don't redistribute raw review text. The Tribe REST API is a standard public
WordPress endpoint (eagleriver.org sits behind Cloudflare — the fetcher sends a
browser `User-Agent`, which the API accepts).

**OpenStreetMap** (`quickbeam data osm`) is the ToS-clean alternative for POIs,
hours, contact info, and categories — no reviews/ratings, but its output feeds the
*same* `Business`/`Category`/`Locality` schema. The node/edge model is
source-agnostic, so OSM (or any new source) can fill these types later without
touching the schema or the build path.
