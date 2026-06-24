# Events (Eventbrite + Tribe) Pipeline

Add **upcoming and historic events** to the local demo, *merged into the same
graph as the bars* — so opening Shotskis the bar shows its events, and a semantic
search like "live music this summer" returns events ranked alongside places. This
is the events twin of `PLACES_GUIDE.md`: a new pair of node types, **not** a new
architecture. Two free, public, no-API-key scrapers feed one raw store; one shaper
emits Event/Organizer nodes (reusing Category/Locality) into the same
`stage_volumes/`, linked to the venue Business by coordinate match.

```
 ┌────────────────────────┐   ┌────────────────────────┐
 │ Eventbrite organizer   │   │ The Events Calendar     │   events-fetch
 │ __NEXT_DATA__ + /org/   │   │ (Tribe) /wp-json/tribe  │   (two --source modes)
 │ {id}/showmore (past)   │   │ /events/v1/events       │
 └───────────┬────────────┘   └───────────┬────────────┘
             ▼                             ▼
        ┌──────────────────────────────────────┐  events_raw (jsonb)  …or JSONL
        └──────────────────┬───────────────────┘
                           ▼  eventspg  (raw → typed graph, MERGED with places)
        ┌──────────────────────────────────────┐
        │ volume_2_events.json / _organizers    │  Event / Organizer (+ Category,
        │ volume_2_edges.json                   │  Locality)  edges: hostedBy,
        │                                       │  inCategory, locatedIn,
        │   hostedAt  Event → Business  ◄────────┼─ hostsEvent Business → Event
        └──────────────────┬───────────────────┘   (coordinate match to the bar)
                           ▼  schemagen --volume 0  →  build / prebake  →  cdn bake
        ┌──────────────────────────────────────┐
        │  bars shard: Businesses + Events      │  one shard, browser semantic search
        └──────────────────────────────────────┘
```

Both sources are **free and need no API key**. We store event *metadata* only
(title, time, venue, price, organizer) — not third-party review text — and treat
the raw store as a short-lived prototype cache.

---

## Stage A — scrape into the raw store (`events-fetch`)

Two sources behind one CLI, writing to the Postgres `events_raw` table.

**Database setup (one time).** The `events_raw` *table* auto-creates on first
connect — but only inside a database that already exists. Postgres cannot
auto-create the *database* (you can't connect to a database that isn't there, so
the `CREATE TABLE` never runs — that's the `database "events_db" does not exist`
error). Two ways to give it a home:

```bash
# RECOMMENDED — reuse the existing places database. `events_raw` is created
# inside it automatically, right next to `places_raw`. No superuser needed.
export EVENTS_PG_DSN="postgresql://places:places@localhost:5432/places_db"

# OR — a dedicated events database (needs a Postgres SUPERUSER; the `places`
# login role can't CREATE DATABASE). Adjust host/port/superuser to your install:
psql "postgresql://postgres@localhost:5432/postgres" \
  -c "CREATE ROLE events LOGIN PASSWORD 'events';" \
  -c "CREATE DATABASE events_db OWNER events;"
export EVENTS_PG_DSN="postgresql://events:events@localhost:5432/events_db"
```

`events-fetch`/`eventspg` read `EVENTS_PG_DSN` (or take `--dsn`). With it set,
the commands below write straight to Postgres — no flags needed.

```bash
# Eventbrite organizer (slug or URL). Upcoming events come from the page's
# __NEXT_DATA__; past events + pagination from the internal show-more JSON API.
quickbeam data events-fetch --source eventbrite \
  --organizer shotskis-29817730199

# The Events Calendar (Tribe) site — the public WP REST API, paginated.
quickbeam data events-fetch --source tribe \
  --site https://eagleriver.org --max-events 200

# Eventbrite LOCATION discovery — find events across the whole area at once,
# without knowing any organizer ids. Pages eventbrite.com/d/<place>/all-events/
# (window.__SERVER_DATA__); every event carries its venue + coordinates, so the
# downstream coordinate match attaches each to whichever bar it happens at.
quickbeam data events-fetch --source eventbrite-location \
  --place wi--eagle-river
#   add --expand-past to also pull each discovered organizer's past events.
```

Re-running is idempotent — rows upsert by `event_key`, so re-sweeping an area
only refreshes what changed. (Prefer no database at all? Append the raw rows to a
file instead with `--no-db --raw-out events.jsonl`, then feed `eventspg --raw-in`.)

> **Do I have to scrape each bar by hand?** No. Use `eventbrite-location`: one
> area scrape (~1 request per 20 events) returns every public upcoming Eventbrite
> event in the region with its venue coordinates, and `eventspg`'s coordinate
> match (`hostedAt`, ≤`--match-radius-m`) links each to the matching `Business`.
> Bars with no Eventbrite presence simply get no events — that *is* the
> "which bars have events?" answer. The `--place` slug comes from an
> `eventbrite.com/d/<state>--<city>/` URL. Note the discovery radius is broad
> (it spans neighbouring towns), so most returned venues won't match your bar
> list — only the ones within `--match-radius-m` of a Business do. Per-organizer
> `--source eventbrite` remains the way to pull one venue's *full* history.

Eventbrite past events omit the venue but carry `venue_id`; the fetcher backfills
them from the organizer's other events (an organizer reuses its venues), so even
historic events get coordinates. `--dry-run` fetches and reports counts without
storing anything.

> ToS: Eventbrite restricts long-term caching of its content; this stores event
> metadata only and is a prototype cache. The Tribe REST API is a standard public
> WordPress endpoint. eagleriver.org sits behind Cloudflare — the fetcher sends a
> browser `User-Agent`, which the API accepts.

## Stage B — raw store → graph, merged with places (`eventspg`)

```bash
# reads events_raw from Postgres (EVENTS_PG_DSN / --dsn) by default
quickbeam data eventspg --output-dir ./stage_volumes
# (or from a JSONL file: quickbeam data eventspg --raw-in events.jsonl --output-dir ./stage_volumes)
```

Writes `volume_2_*` alongside the places `volume_1_*` (schemagen `--volume 0`
reads both). The **`hostedAt` / `hostsEvent`** edges link each Event to the
nearest existing `Business` within `--match-radius-m` (default 120 m; Shotskis'
Eventbrite venue is ~12 m from its Google Place), falling back to a venue-name /
business-title match. The Business index is read from
`<output-dir>/volume_1_businesses.json` (override with `--businesses-in`); events
with no match still stand alone.

| File                       | Node type   | Key fields                                                                                                                                                    |
| -------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `volume_2_events.json`     | `Event`     | title, dateLabel, startDate, venueName, address, coordinates, locality, priceLevel, ticketUrl, organizerName, isPast, isCancelled, categories, source, `text` |
| `volume_2_organizers.json` | `Organizer` | title, bio, website, facebook, source                                                                                                                         |

## Stage C — schema, embeddings, shard

```bash
# 1) infer schemas across BOTH volumes (one bundle covering bars + events).
quickbeam data schemagen --input-dir ./stage_volumes --volume 0 \
  --prefix fangorn.places --bundle-name localcore --version v1
```

`--volume 0` is the new "all volumes" mode: it merges node types that appear in
several volumes (e.g. Category) and combines every edges file, so the
`fangorn.places.localcore.v1` bundle gains the `Event`/`Organizer` node schemas
and the `hostedBy` / `hostedAt` / `hostsEvent` edge shapes.

Then embed + bake. Two paths:

**Production (on-chain):** register the updated node schemas + bundle via the
Fangorn SDK (node schemas first, then the bundle), then:

```bash
quickbeam build --bundle fangorn.places.localcore.v1=0x<bundleId> \
  --root-profile business --root-profile localevent
quickbeam cdn bake --domain bars
```

`embeddings.py` now folds `Event` into the `business` profile (a bar's document
mentions its events) and adds a `localevent` root profile (one document per
Event, folding in its venue Business, organizer, category, locality).

**Local demo (no chain) — `prebake`:** embed the local volume node files straight
into Qdrant with the exact same `nomic-embed-text-v1.5` + matryoshka-256 recipe
`build` uses, so vectors are comparable to build-time and to the in-browser query
vectors:

```bash
# embed events + organizers into the existing collection, and fold each bar's
# hosted-event titles into its payload (payload-only — bar vectors untouched).
quickbeam data prebake --input-dir ./stage_volumes --volume 2 \
  --types Event,Organizer --collection fangorn --link-events

quickbeam cdn bake --domain bars      # re-bake the shard with events merged in
```

## Stage D — the demo

`domains.json` (and the qdrant-mode overlay `examples/public/domain.json`) put
`Event` in the bars domain filter and add presentation for `Event`/`Organizer`.
The examples app renders an **event-flavored entity page** (Upcoming/Past badge,
date, venue with a `◎ Nearby` coordinate-proximity link, price, a **Tickets ↗**
link, and the organizer), a bar lists its events under Connections (via the
folded `events` field), and the results rail gains an **"Upcoming events only"**
toggle that drops past events while leaving bars unaffected.

```bash
cd examples && npm run build      # tsc + vite
```

## Verify

```bash
# events are searchable and rank for a semantic query
#   "live music this summer" → LOLA Live, Marina Bar music line-up, …
# the merge link
grep -c '"rel": "hostedAt"' stage_volumes/volume_2_edges.json     # > 0
# upcoming filter keeps bars, drops past events (Qdrant must_not on fields.isPast)
```

Re-running any stage is idempotent (upsert by id / event_key), and only Stage A
touches the network — `eventspg`, `schemagen`, `prebake`, and `bake` all reprocess
the cache for free.

## Adding more sources

The node/edge model is source-agnostic. A new event source just needs a
`normalize_*` function in `events_pg.py` returning the common event dict; the
shaping, merge link, schema, and UI are unchanged.
