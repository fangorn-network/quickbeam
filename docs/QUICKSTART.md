# Quickstart — Local Discovery (commands only)

Terse command runbook for the Places + Events pipeline. Prose, ToS, cost guards,
and the "why" live in [`LOCAL_DISCOVERY_GUIDE.md`](./LOCAL_DISCOVERY_GUIDE.md). New
here? Read [`PIPELINE_STAGES.md`](./PIPELINE_STAGES.md) first — it names the bronze →
silver → gold stages every command below belongs to.

Conventions: `quickbeam …` runs in the **quickbeam** repo; `publish_bundle.ts` runs
in the **fangorn** repo (`~/fangorn/fangorn`). `STAGE=~/fangorn/embeddings/stage_volumes`.

```bash
export STAGE=~/fangorn/embeddings/stage_volumes
```

---

## 0. Setup (once)

```bash
# only if scraping Google Places
export GOOGLE_PLACES_API_KEY=AIza...

# publish_*.ts (fangorn repo) read these — or ~/.fangorn/config.json (`fangorn init`) / ~/fangorn/.env
export DELEGATOR_ETH_PRIVATE_KEY=0x... PINATA_JWT=... PINATA_GATEWAY=... CHAIN_NAME=base-sepolia

# `quickbeam build` reads the subgraph + an IPFS gateway. Set once; reused as $BUILD_AUTH below.
export GRAPH_API_KEY=...                              # The Graph gateway key
export IPFS_GATEWAY=https://<your-gateway>/ipfs       # e.g. a dedicated Pinata gateway
export IPFS_GATEWAY_KEY=...                           # gateway JWT (omit if your gateway is public)
BUILD_AUTH="--graph-api-key $GRAPH_API_KEY --ipfs-gateway $IPFS_GATEWAY --ipfs-gateway-key $IPFS_GATEWAY_KEY"
```

Run qdrant and postgres
``` sh
# qdrant
docker run -d -p 6333:6333 -p 6334:6334 \
  -v "$(pwd)/python/qdrant_storage:/qdrant/storage:z" \
  --name qdrant-core \
  qdrant/qdrant

# postgres
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

---

## A. One-shot demo — single merged bundle (you own both domains)

```bash
# scrape (Stage A) — events are free; Google Places costs money (try --dry-run first)
quickbeam data places-fetch --location 45.917,-89.244 --radius 10000 \
  --types "bar,restaurant,night_club" --sweep --anchor "Shotskis" --dry-run
quickbeam data events-fetch --source eventbrite-location --place wi--eagle-river

# shape → stage_volumes (Stage B): places → volume_1_*, events → volume_2_*
quickbeam data placespg --output-dir $STAGE
quickbeam data eventspg --output-dir $STAGE

# schema (Stage C) — one bundle over ALL volumes
quickbeam data schemagen --input-dir $STAGE --volume 0 \
  --prefix eagleriver.sond3r.com --bundle-name localcore --version v1

# publish to Fangorn (fangorn repo) → prints bundle id 0x<bundleId>
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir $STAGE --volume 0

# embed + bake (Stage C/D) — it's a bundle, so --bundle
quickbeam build --bundle "eagleriver.sond3r.com.localcore.v1=0x<bundleId>" $BUILD_AUTH \
  --root-profile business --root-profile review --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors

# demo (Stage D) — VITE_LOCALE=de-hofheim for the German build
cd examples && VITE_DATA_SOURCE=shards VITE_DOMAIN=places VITE_LOCALE=en-eagle-river npm run dev
```

> Broader sweep net: add more `--types` (e.g. `store,cafe,bakery,grocery_store,pharmacy,bank,gas_station,brewery,tourist_attraction,museum,park,…`) to the `places-fetch` above.

---

## B. One datasource per source — fused by a View

**The principle.** Each independent source becomes **one datasource, published once**.
Many datasources can reuse one schema. A **View** lists their resourceIds and fuses
them. **Adding or updating one source never re-publishes another** — that is the whole
point of this layout versus section A's single merged bundle.

Two operations, kept distinct:
- **Add** a source → a *new* datasource (B2 / "add another source"). Nothing existing is touched.
- **Update** a source → re-publish *that one* datasource → a new version (section C).

> Two sources describe the **same real things** with no shared id (Google Places
> `gplace:` vs OSM `osm:`)? They'll appear as duplicates until you assert the join.
> See **[`FUSING_SOURCES.md`](./FUSING_SOURCES.md)** — generate a `sameAs` linkset
> and the View collapses each pair into one fused entity.

> Schema names (`<prefix>.<type>.<version>`) are **immutable once registered**. The OSM
> `business` shape ≠ section A's Google `business`, so OSM uses its own `…osm` prefix.
> Every event source reuses **one** event schema (`…evt`); the sources stay separate as
> distinct **datasources** via `--dataset`, not as distinct schemas.

### B1. Places (OSM) — one datasource  *(you already did this)*

```bash
quickbeam data osm --place wi--eagle-river --volume 3 --output-dir $STAGE
quickbeam data schemagen --input-dir $STAGE --volume 3 \
  --prefix eagleriver.sond3r.com.osm --bundle-name placecore --version v1
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir $STAGE --volume 3
#   ✅ … resourceId 0x<R_places>   ← the OSM places datasource (printed by the script)
```

### B2. Events (Tribe) — a SECOND datasource, its own data

Keep each event source in its **own volume** so shaping one never mixes in another.
Fetch tribe-only raw, shape it into a fresh volume, and publish it as the `tribe`
**dataset** under a single shared event schema:

```bash
# tribe-only raw → its own JSONL (so eventspg can't blend it with another source)
quickbeam data events-fetch --source tribe --site https://eagleriver.org \
  --no-db --raw-out tribe_events.jsonl
quickbeam data eventspg --raw-in tribe_events.jsonl --volume 4 --output-dir $STAGE

# the event SCHEMA — reused by every event source; registered once
quickbeam data schemagen --input-dir $STAGE --volume 4 \
  --prefix eagleriver.sond3r.com.evt --bundle-name eventcore --version v1

# publish tribe as its OWN datasource under that schema. --dataset makes it distinct
# from any other event source that reuses the same schema.
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir $STAGE --volume 4 --dataset tribe
#   ✅ … dataset: tribe … resourceId 0x<R_tribe>   ← the script also prints the view build line
```

### B3. Fuse with a View, then build

```bash
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_view.ts \
  --name eagleriver.sond3r.com.localview.v1 \
  --source-bundle eagleriver.sond3r.com.osm.placecore.v1 \
  --source-bundle eagleriver.sond3r.com.evt.eventcore.v1:tribe
#   ✅ … view id 0x<viewId>   ← the script prints the exact `quickbeam build --view` line too

quickbeam build --view "eagleriver.sond3r.com.localview.v1=0x<viewId>" $BUILD_AUTH \
  --profiles-file ~/fangorn/embeddings/osm_profiles.json \
  --root-profile business --root-profile localevent \
  --root-profile lake --root-profile trail --root-profile landmark --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

> **A profile = which node types get emitted as documents.** A type with no
> `--root-profile` is fused into the graph but never projected → invisible. OSM's
> `Lake`/`Trail`/`Landmark` aren't built-in profiles, so `osm_profiles.json` defines
> them and the three `--root-profile` flags select them. Drop them to omit nature types.

> **Just pass bundle names.** `--source-bundle <name>` uses the default dataset
> (places). For a `--dataset`-named datasource, append the label:
> `--source-bundle <name>:tribe` — the script resolves the resourceId for you. You
> only need `--source-resource 0x<rid>` for a datasource published by **another
> wallet** (section D), which you can't name.

### Add ANOTHER event source later (e.g. Eventbrite) — Tribe untouched

Repeat B2 with a different source + dataset. OSM and Tribe are **not** re-published:

```bash
quickbeam data events-fetch --source eventbrite-location --place wi--eagle-river \
  --no-db --raw-out eb_events.jsonl
quickbeam data eventspg --raw-in eb_events.jsonl --volume 5 --output-dir $STAGE
quickbeam data schemagen --input-dir $STAGE --volume 5 \
  --prefix eagleriver.sond3r.com.evt --bundle-name eventcore --version v1   # SAME schema
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir $STAGE --volume 5 --dataset eventbrite

# a View's source set is fixed at registration, so adding a source = a new view version
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_view.ts \
  --name eagleriver.sond3r.com.localview.v2 \
  --source-bundle eagleriver.sond3r.com.osm.placecore.v1 \
  --source-bundle eagleriver.sond3r.com.evt.eventcore.v1:tribe \
  --source-bundle eagleriver.sond3r.com.evt.eventcore.v1:eventbrite
```

---

## C. Update an existing source (more data, SAME source)

When *Tribe itself* posts new events, re-publish **only the Tribe datasource** — same
volume, same `--dataset` ⇒ a new **version** at the same `R_tribe`. No other source is
touched; the View needs no re-registration (it auto-resolves the newest manifest).

```bash
quickbeam data events-fetch --source tribe --site https://eagleriver.org \
  --no-db --raw-out tribe_events.jsonl                       # idempotent upsert by event_key
quickbeam data eventspg --raw-in tribe_events.jsonl --volume 4 --output-dir $STAGE
quickbeam data schemagen --input-dir $STAGE --volume 4 \
  --prefix eagleriver.sond3r.com.evt --bundle-name eventcore --version v1
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir $STAGE --volume 4 --dataset tribe              # SAME dataset ⇒ new version of R_tribe
quickbeam build --view "eagleriver.sond3r.com.localview.v1=0x<viewId>" $BUILD_AUTH \
  --profiles-file ~/fangorn/embeddings/osm_profiles.json \
  --root-profile business --root-profile localevent \
  --root-profile lake --root-profile trail --root-profile landmark --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

---

## D. Claimed business profile (self-sovereign, owner-published)

```ts
// the BUSINESS runs this from THEIR wallet → distinct owner ⇒ distinct resourceId
await fangorn.schema.register({
  name: "fangorn.places.businessProfile.v1",
  definition: {
    placeId: { "@type": "string" }, officialName: { "@type": "string" },
    hours: { "@type": "string" }, description: { "@type": "string" },
    menuUrl: { "@type": "string" }, updatedAt: { "@type": "string" },
  },
  identity: { "@id": "placeId", aliases: { gplace: "placeId" } },   // shares gplace ⇒ fuses onto Business
});
await fangorn.publisher.publishBundle({
  bundleName: "fangorn.places.businessProfile.v1", datasetName: "shotskis",
  nodes: [{ id: "ChIJ...shotskis", type: "BusinessProfile", fields: {
    placeId: "ChIJ...shotskis", officialName: "Shotskis Bar & Grill",
    hours: "Mon–Sun 11:00–02:00", description: "Lakeside supper club; live music Fri.",
    menuUrl: "https://shotskis.example/menu", updatedAt: "2026-06-30",
  }}],
});
// no shared alias to fuse on? assert the join with a `sameAs` linkset — see
// docs/FUSING_SOURCES.md (e.g. fusing Google Places ⟷ OSM businesses).
```

Wire the profile into a view (its source set is fixed at registration, so a new
source = a new view version), then rebuild:

```bash
cd ~/fangorn/fangorn && pnpm dotenvx run -f .env -- tsx src/test/publish_view.ts \
  --name eagleriver.sond3r.com.localview.v3 \
  --source-bundle eagleriver.sond3r.com.osm.placecore.v1 \
  --source-bundle eagleriver.sond3r.com.evt.eventcore.v1:tribe \
  --source-resource 0x<profileResourceId>          # foreign wallet ⇒ pass its resourceId
quickbeam build --view "eagleriver.sond3r.com.localview.v3=0x<viewIdV3>" $BUILD_AUTH \
  --profiles-file ~/fangorn/embeddings/osm_profiles.json \
  --root-profile business --root-profile localevent \
  --root-profile lake --root-profile trail --root-profile landmark --reset
```

Owner edits = re-run that `publishBundle` (same name + datasetName) → new version,
same resourceId. Rebuild the view to pick it up.

---

## Verify

In the served demo (`quickbeam cdn serve` → the examples app), search:
- `"live music this summer"` → events surface alongside places
- `"tacos"` → the business a review raved about (review text is searchable)

For the View build, the console also prints `resolved N/N source(s)` — every declared
source should resolve to a manifest.
