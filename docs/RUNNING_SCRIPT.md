# Running `build_place.py` — Interactive Local Discovery Builder

`build_place.py` (at the repo root) builds a Local Discovery semantic-search shard
for **any** place through one interactive session. It wraps the whole `quickbeam`
pipeline:

```
geocode -> scrape (Places + Events) -> graph -> embed -> bake -> demo
```

For the concepts behind each stage — the data model, the on-chain `build`
alternative, and more — see
[`docs/LOCAL_DISCOVERY_GUIDE.md`](LOCAL_DISCOVERY_GUIDE.md).

> **Nothing is billable until you confirm.** The script always runs the Google
> Places **dry-run** first and makes you explicitly approve the one paid step.
> All Search calls are free; only Place Details is billed (~$0.025 per unique
> business).

---

## Prerequisites

| Need | Why | Set up |
|---|---|---|
| Python venv with the CLI | runs `quickbeam` | `python -m venv venv && source venv/bin/activate && pip install -e ".[gpu]"` (or `.[cpu]`) |
| Qdrant running | vector store | `docker run -d -p 6333:6333 -p 6334:6334 --name qdrant-core qdrant/qdrant` |
| Postgres running | raw cache for `placespg`/`eventspg` | see [Postgres setup](#postgres-setup) below |
| `GOOGLE_PLACES_API_KEY` | only if scraping places | `export GOOGLE_PLACES_API_KEY=AIza...` (Places API **new** + billing on) |
| Node 18+ | only for the demo UI | already in `examples/places/` |
| Internet | geocoding via OSM Nominatim (free) | falls back to manual `lat,lng` entry if offline |

The script checks `quickbeam`, Qdrant, the API key, and `PLACES_PG_DSN` in a
**preflight** step and tells you what's missing before doing any work.

### Postgres setup

Postgres is a raw cache in front of the real targets (Qdrant + the shard), read by
`placespg`/`eventspg` in Stage B. The official `postgres` image creates a
`postgres` superuser and database, **not** the `places`/`places_db` the default DSN
expects, so create those once:

```bash
# trust auth = no password needed
docker run -d --name places -p 5432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres

# create the role + database the DSN points at (idempotent; re-run errors are harmless)
docker exec places psql -U postgres -c "CREATE ROLE places LOGIN PASSWORD 'places';"
docker exec places psql -U postgres -c "CREATE DATABASE places_db OWNER places;"

export PLACES_PG_DSN=postgresql://places:places@localhost:5432/places_db
export EVENTS_PG_DSN=postgresql://places:places@localhost:5432/places_db
```

The `places_raw`/`events_raw` tables auto-create on first use. If you hit
"password authentication failed" or see stale data, you've probably reached a
*host* Postgres on 5432 instead of the container — check `ss -ltnp | grep 5432`
and that the container published its port (`docker port places`).

---

## Run it

```bash
source venv/bin/activate
export GOOGLE_PLACES_API_KEY=AIza...      # only if you'll scrape places
python build_place.py
```

That's it — answer the prompts. Press **Ctrl-C** at any time to abort safely
(every underlying step upserts by id, so a partial run is never corrupting and is
safe to re-run).

### Optional environment overrides

| Variable | Default | Effect |
|---|---|---|
| `QDRANT_URL` | `http://localhost:6333` | Point at a non-local Qdrant |
| `PLACES_PG_DSN` / `EVENTS_PG_DSN` | local `places_db` | Raw-cache database for Stage B |
| `GOOGLE_PLACES_API_KEY` | — | Required only when you choose to scrape places |

---

## What it asks (in order)

1. **Stages** — Scrape places? (costs money) · Scrape events? (free).
2. **Location** — a place name (e.g. `Jackson, MS`, `Lisbon, Portugal`). It
   geocodes to a **sweep center** and an **events bbox** automatically; confirm or
   override. No match / offline → enter `lat,lng` (and optionally a `W,S,E,N` bbox)
   by hand.
3. **Output targets** — Qdrant collection (default `fangorn`), CDN dir
   (default `./cdn`), and domain to bake (default `places`). Defaults drop straight
   into the existing demo wiring; change them for per-place isolation.
4. **Type net** — `wide` (default, ~46 validated Google types), `everyday`,
   `nightlife`, or `custom`. Over-50 lists are auto-trimmed to Google's per-call
   limit.
5. **Sweep + cost controls** — radius, min tile radius (lower digs deeper into
   dense areas), max tiles (free), and **max billable Place Details** (your hard
   spend ceiling).
6. **Text passes** — pre-filled niche queries (record stores, antiques, galleries,
   bookstores, live music) templated with your place name. These catch shops Google
   has **no type** for; edit or blank them out.
7. **Workspace** — offers to clear a stale `stage_volumes/` so you don't mix
   localities into one shard.
8. **Plan summary → Proceed?** Then it runs Stage A → D.

During Stage A it runs the **dry-run**, shows the billable tally, and asks again
before the real (paid) scrape — defaulting to **No**.

---

## Example session (abridged)

```text
$ python build_place.py

  Local Discovery shard builder — any place, interactive

? Scrape Google Places (businesses)? (needs API key, costs money) [Y/n]: y
? Scrape events (Eventbrite, free)? [Y/n]: y

== Preflight ===========================================
✓ quickbeam: /home/me/embeddings/venv/bin/quickbeam
✓ Qdrant reachable at http://localhost:6333

== Location ============================================
? Place name [Jackson, MS]: Asheville, NC
  resolved: Asheville, Buncombe County, North Carolina, United States
    center : 35.59543,-82.55092
    bbox   : -82.6703,35.4164,-82.4604,35.6560   (W,S,E,N — used for events)
? Use these? [Y/n]: y
...
? Type net [wide]: wide
? Sweep radius (meters) [12000]: 12000
? Max billable Place Details (HARD spend ceiling) [500]: 150
...
== Stage A1 — Places dry-run (no charge, no storage) ==
$ quickbeam data places-fetch --location 35.59543,-82.55092 ... --dry-run
Dry run — a real run would make 150 billable Place Details call(s) ...
? This is the ONLY billable step. Run the REAL Places scrape now? [y/N]: y
...
== Done ================================================
✓ baked 412 points: [('Business', 318), ('Event', 94)]

Launch the demo (two terminals):
  quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
  cd examples/places && VITE_DATA_SOURCE=shards ... npm run dev
```

---

## After it finishes — launch the demo

The script prints the two commands (it does not start long-running servers
itself). In two terminals:

```bash
# Terminal 1 — serve the baked shard (CORS required for the browser)
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors

# Terminal 2 — run the app, pre-filled with the place's community labels
cd examples/places && VITE_DATA_SOURCE=shards VITE_CDN_URL=http://localhost:8090 \
  VITE_DOMAIN=places VITE_COMMUNITY_NAME="Asheville" VITE_COMMUNITY_REGION="North Carolina" \
  VITE_COMMUNITY_REGION_ABBR="NC" VITE_COMMUNITY_SLUG="asheville" npm run dev
```

Open the URL it prints (`http://localhost:5173`, or `5174` if taken). After a
re-bake, just **reload** the browser — `cdn serve` reads the new shard from disk.

---

## Cost safety

- **Free:** all Nearby/Text Search calls (IDs-only field mask = Google Essentials).
- **Billable:** Place Details, once per unique business (~$0.025; ~$25/1,000),
  often $0 under Google's monthly free allotment.
- The script **always dry-runs first** and the real scrape defaults to **No**.
- `Max billable Place Details` is a per-run hard ceiling.
- The only thing that truly stops spend is a **daily quota cap** in Google Cloud →
  APIs & Services → *Places API (new)* → Quotas (over-limit calls are rejected,
  not billed).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `quickbeam not found` | Activate the venv: `source venv/bin/activate`. |
| `Qdrant not reachable` | Start the container (see Prerequisites). |
| Geocoding returns no match | Use a more specific name (`City, ST`), or enter `lat,lng` manually when prompted. |
| Events report `Saved 0 events` / a `405` page | Eventbrite anti-bot block — not your input. Events are optional; retry later/another network, or proceed businesses-only. |
| `Collection ... doesn't exist!` | Shouldn't happen via the script (it creates the collection). If running steps by hand, create it first (size 256 / Cosine / on_disk): `curl -X PUT http://localhost:6333/collections/fangorn -H 'Content-Type: application/json' -d '{"vectors":{"size":256,"distance":"Cosine","on_disk":true}}'` |
| A known niche shop is missing | Add a **text pass** for it, or fetch it directly: `quickbeam data places-fetch --query "Name, City ST"` (or `--details-only "<place_id>"`), then re-run `placespg → prebake → cdn bake`. |
| Embeddings log `libcudnn.so.9: cannot open ...` | Harmless — fell back to CPU (fine for a few hundred nodes). GPU fix: the `LD_LIBRARY_PATH` export in the project `README.md`. |
| Shard mixes two cities | You skipped the "clear `stage_volumes/`" prompt — clear `stage_volumes/volume_*.json` and re-run Stage B onward. |

---

## Notes & limitations

- **The script encodes the easy-to-miss steps for you:** it creates the Qdrant
  collection (size 256 / Cosine / on_disk) before embedding, embeds businesses
  *before* events (so the `--link-events` fold has business payloads to write
  into), and passes the events bbox in the `--bbox=` equals-form so a leading `-`
  isn't read as a flag.
- **Prebake path tradeoff:** this builds the shard with the offline `prebake`
  recipe, which keeps `entityType` in `{Business, Event}` and does **not** fold
  review text into business documents (that folding only happens in the on-chain
  `build` path). So a review-only term like "tacos" may not surface its business;
  businesses are still searchable by name, type, locality, summary, rating, and
  amenities. See [`docs/LOCAL_DISCOVERY_GUIDE.md`](LOCAL_DISCOVERY_GUIDE.md) for
  the on-chain alternative.
- **Demo chrome vs. data:** `VITE_COMMUNITY_*` overrides only the name/region
  labels. The "vibe" quick-searches and microcopy come from `VITE_LOCALE`
  (default `en-eagle-river`), and the browser tab title is hard-coded in
  `examples/places/index.html`. For a fully localized voice, author a locale profile in
  `examples/places/src/lib/i18n/` and pass `VITE_LOCALE=<id>`.
- **Re-running is safe:** every step upserts by id (`place_id` / `event_key`), and
  only the Places scrape touches the paid API — downstream stages reprocess the
  local cache for free.

## See also

- [`docs/LOCAL_DISCOVERY_GUIDE.md`](LOCAL_DISCOVERY_GUIDE.md) — concepts, data
  model, and the on-chain `build` alternative.
- [`docs/SEMANTIC_CDN.md`](SEMANTIC_CDN.md) — how baked domains/shards work.
