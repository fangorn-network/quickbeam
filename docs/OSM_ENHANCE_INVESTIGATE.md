# Multi-Source Enrichment — Investigation Plan

**Status:** investigate later. Captured from the Places-pipeline exploration.

**Goal:** make the Eagle River dataset *richer* by ingesting multiple free data
sources and synthesizing them into **one connected graph** — without spending
money and (mostly) without credit cards. The downstream pipeline
(`placespg`-style shaper → `schemagen` → `build` → `cdn`) is reused unchanged;
each new source is just a small **fetch + shape** adapter.

> Why this matters: richness comes from *more node types that cross-link through
> shared anchors*, not just more businesses. A query like *"lakefront supper club
> with live music near the snowmobile trails"* only resolves if bars, lakes,
> trails, events, and local history all live in one graph.

---

## Core mental model (confirmed)

Any source → raw rows in Postgres → existing scripts shape it → schemagen →
build. The spine never changes. The synthesis happens because every source
attaches to the same hub nodes:

- **Locality** "Eagle River, WI" — the gravitational center (Census enriches it,
  Wikipedia describes it, every business `locatedIn` it, every event `heldIn` it).
- **Coordinates → `near` edges** — spatial proximity links a bar, a lake, a
  trailhead, and a festival regardless of source.
- **Categories** — a shared taxonomy across sources.

---

## Proposed storage refactor (multi-source)

Today `places_raw` is Google-shaped. Generalize to a source-agnostic raw table:

```sql
raw_entities (
    source      text,        -- 'osm' | 'wikidata' | 'census' | 'yelp' | 'google' | ...
    source_id   text,        -- stable id within that source
    kind        text,        -- 'business' | 'lake' | 'event' | 'landmark' | ...
    payload     jsonb,       -- verbatim source response
    fetched_at  timestamptz DEFAULT now(),
    PRIMARY KEY (source, source_id)
);
```

- One **shaper per source** maps `payload → common node types`, all emitting into
  the **same** `volume_*.json` files.
- `schemagen` infers the union schema automatically (it already does per-type
  inference); `build` embeds it. No spine changes.
- Keep `places_raw` working (or migrate it in as `source='google'`).

---

## The one genuinely hard part: entity resolution

If Shotski's appears in Google **and** OSM **and** Wikidata, we want **one**
`Business` node, not three. Build a **dedup pass in the synthesizer**:

- Merge key: proximity (within ~75 m) + loose name match (substring or token
  overlap), falling back to normalized name + locality when coords are missing.
- On merge: fill scalar gaps, union categories/amenities/reviews, keep the
  higher-vote rating, preserve provenance (`sources`, per-source ids).
- Without it: duplicate nodes pollute the embeddings.

> Note: this was prototyped once (the Google + Yelp `NormBiz`/merge version of
> `places_pg.py`) and proven to work — Google + Yelp Shotski's collapsed into one
> node. It was reverted when **Yelp retired its free Fusion tier** (now a paid
> "Yelp Places API" trial), leaving `places_pg.py` Google-only again. When the
> first genuinely-free second source (OSM) lands, reintroduce the `NormBiz` parse
> layer + dedup pass — each source then just needs a `parse_<source>() -> NormBiz`.

---

## Free source menu (mapped to node types)

### Geographic / POI
- **OpenStreetMap / Overpass** — *no key.* Bars, restaurants, shops, parks, boat
  launches, **snowmobile trails**, the **Chain of Lakes**.
  → `Business`, `Landmark`, `Trail`, `Lake`, `Category`, `near` edges.
  Base already exists: `quickbeam/pipelines/osm.py`.

### Encyclopedic / structured facts
- **Wikidata (SPARQL)** + **Wikipedia REST** — *no key.* Eagle River the town
  (population, Vilas County, founded), **Carl Eliason / snowmobile history**, the
  **World Championship Snowmobile Derby**, notable landmarks.
  → rich `Locality`, `Landmark`, `Event`, `Person`.
- **Wikivoyage** — *no key.* Travel-guide prose ("eat / drink / see"). Excellent
  embedding fuel. → descriptive text on `Business`/`Attraction`.

### Events (ties into the ticketing angle)
- **Ticketmaster Discovery API** — *free key, no card.* Events by geo/city.
  → `Event` linked to its venue `Business` via `heldAt`.
- **MusicBrainz** — already have `mb_pg.py` emitting `Place`/`Event`/`Artist`;
  local venues that exist in MB link straight in.

### Demographic / civic context
- **US Census / ACS API** — *free key, no card.* Population, income, age for
  Eagle River / Vilas County. → enriches `Locality`.
- **NWS weather (api.weather.gov)** / **Wisconsin DNR lake data** — *no key.*
  Seasonal/forecast context, lake names/depths. → `Lake`, environmental fields.

### Reviews / ratings (free alternatives to paid Google)
- **Yelp** — ❌ **NOT free anymore.** Yelp retired the free Fusion tier; it's now a
  paid **"Yelp Places API"** (trial → paid). A scraper was prototyped and removed.
  Skip unless you're willing to pay. (Reviews are Google's edge for now.)
- **Foursquare Places** — *free tier* may still exist; verify current terms before
  building. Tips/categories.

### Media (visual punch for the demo)
- **Wikimedia Commons / Flickr** — *free.* Geotagged photos near Eagle River.
  → media fields on nodes.

---

## Recommended starter set (all no-card, maximally Eagle-River-flavored)

1. **OSM / Overpass** — spatial backbone (businesses, lakes, trails). No signup.
2. **Wikidata + Wikipedia / Wikivoyage** — local color (derby, snowmobile
   history, Chain of Lakes). No signup.
3. **Census** — Locality enrichment. Free key.

This trio yields a rich, connected Eagle River graph with **zero spend and zero
credit card**, and proves the multi-source synthesis end to end. Layer
Google/Yelp reviews on top later.

---

## Build order (when we pick this up)

- [ ] **(a)** Generalize the raw store to `raw_entities` (keep `places_raw` path
      working or migrate as `source='google'`).
- [ ] **(b)** Write the **synthesizer**: read all raw tables → common node/edge
      shapers → unified `volume_*.json`, with the dedup/entity-resolution pass.
- [ ] **(c)** Build the **OSM adapter** first (no key) against `raw_entities` —
      the template the others follow.
- [ ] **(d)** Add **Wikidata/Wikipedia** adapter (richest local context, no key).
- [ ] **(e)** Add **Census** adapter (Locality enrichment).
- [ ] **(f)** Run `schemagen` → confirm union schema; `build --root-profile
      business` → bake shard.
- [ ] **(g)** Later: Yelp/Google reviews, Ticketmaster events, media.

### Open questions to resolve later
- One generic `raw_entities` table vs. one table per source? (Leaning generic.)
- Where does dedup live — in the synthesizer, or a separate materialized pass?
- New node types (`Lake`, `Trail`, `Landmark`, `Event`, `Person`) → do they each
  need a `business`-style root profile, or fold into the Business/Locality views?
- Cross-source category taxonomy mapping (OSM `amenity=bar` vs Yelp category vs
  Google type) — normalize to one `Category` vocabulary?

---

## Related files
- `quickbeam/pipelines/places.py` — Google Places scraper (the adapter template).
- `quickbeam/pipelines/places_pg.py` — payload → node/edge shaper (`run_export`,
  `iter_jsonl_rows`, `iter_db_rows`).
- `quickbeam/pipelines/osm.py` — existing OSM changeset fetcher (base for the POI
  adapter).
- `quickbeam/pipelines/mb_pg.py` — the declarative-registry pattern to mirror.
- `quickbeam/pipelines/fangorn_schema.py` — `schemagen` (union schema inference).
- `quickbeam/embeddings.py` — `ROOT_PROFILES` (the `business` profile lives here).
- `docs/LOCAL_DISCOVERY_GUIDE.md` — end-to-end Places + Events pipeline walkthrough.
