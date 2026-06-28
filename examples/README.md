# Schema Browser

Eagle River
├── businesses
├── bars
├── events
├── trails
├── lakes
├── fishing
├── snowmobiling
├── ATV routes
├── festivals
└── lodging

A wiki-style, **schema-agnostic** browser for any Fangorn Qdrant collection. Dark,
information-dense, keyboard-first. React + Vite + TypeScript + plain CSS Modules. No
UI framework.

It ships configured for the MusicBrainz corpus, but nothing in the code is
music-specific: entity types, field labels, the per-type icons/colors, the
relationship vocabulary, and the one-line summaries are all **derived at runtime**
from a `Domain` (see [Domain model](#domain-model)). Point it at a recipes or movies
collection and it browses that instead, with no code change.

## Prerequisites

- **Node 18+** (developed on Node 22).
- **No backend required by default** — the app runs against an in-browser mock
  data source (see [Data source](#data-source)), so `npm run dev` just works.
  Point it at a real Qdrant with `VITE_DATA_SOURCE=qdrant` (needs Qdrant at
  `http://localhost:6333` with a `fangorn` collection, reached via the Vite dev
  proxy `/qdrant/*`).

The app is **resilient when the data source is unreachable**: the TopBar shows a
connection-error badge and content areas render inline error states instead of
crashing.

## Data source

The data layer is behind one seam ([`src/lib/qdrant.ts`](src/lib/qdrant.ts)),
selected by `VITE_DATA_SOURCE` ([`src/lib/config.ts`](src/lib/config.ts)):

- **`mock`** (default) — a "fake Qdrant" entirely in the browser
  ([`src/lib/mock.ts`](src/lib/mock.ts)): a generated MusicBrainz-shaped dataset
  with `scroll` / `search` / `count` / `getPoint` / `recommend`. No backend, no
  network. This is what makes the QR-scan-and-explore demo work with zero setup.
- **`qdrant`** — the REST client against a real Qdrant via the dev proxy.
- **`shards`** — download a Semantic CDN snapshot ([`src/lib/shards.ts`](src/lib/shards.ts))
  and search it in-browser. The document vectors are *served* (precomputed by
  `quickbeam build`); the browser never builds embeddings. The CDN manifest is
  self-describing (role_map / entity_types / bundle / presentation baked by
  `quickbeam cdn bake`), so it drives the `Domain` directly.

### Run against a real CDN snapshot (shards mode)

```sh
# 1. (operator) bake a SMALL snapshot — --limit keeps it browser-friendly:
quickbeam cdn bake --config domains.json --collection fangorn \
  --domain music --limit 3000 --cdn-dir ./cdn

# 2. (operator) serve it with CORS so the browser can fetch cross-origin:
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors

# 3. (app) point the browser app at the CDN and run:
VITE_DATA_SOURCE=shards VITE_CDN_URL=http://localhost:8090 npm run dev
#   VITE_DOMAIN=<name> picks a domain; omitted → first in the catalog.
```

The whole snapshot loads into memory on startup; for a few thousand 256-d vectors
that's a few MB and brute-force cosine ("similar entries") is instant — no vector
DB, no WASM.

**No model runs in the browser.** Document embeddings are precomputed server-side
(`quickbeam build`) and *downloaded*; the browser never builds embeddings. The three
capabilities work as:

- **Browse** — paginated scroll by type.
- **"Similar entries"** — cosine over the *served* document vectors
  (`recommend`). This is the meaning-based discovery, and needs no model. The mock
  gives its synthetic vectors topical structure via
  [`src/lib/mockSpace.ts`](src/lib/mockSpace.ts) so "similar" is plausible.
- **Search** — lexical keyword match over names / artists / tags / places.

Free-text semantic search (typing a sentence → embedding the *query*) is the only
thing that would need an in-browser model; it's intentionally out of scope, so there
is no WASM/ONNX dependency. For small snapshots, vector cosine is plain JS over a
`Float32Array` — fast enough that no ANN/WASM library is needed.

## Run

```bash
npm install
# http://localhost:5173
npm run dev      
# tsc -b + vite build (must pass cleanly)
npm run build    
# serve the production build
npm run preview  
# build a static build 
npm run build:statc

npx wrangler login
# deploy the static build (dist) to cloudflare
npx wrangler pages deploy dist --branch main
```


> Note: `npm run preview` and any production deploy do **not** include the
> `/qdrant` dev proxy — that proxy only exists in `vite dev`. For production
> you would point the data layer at a real Qdrant URL / reverse proxy.

## Screens

- `/` — Landing: type-browse grid with live counts, search bar, recent activity
  (sessionStorage).
- `/browse/:entityType` — paginated browse of one entity type (Qdrant scroll).
- `/search?q=&type=` — full-text search results with a left-rail type filter and
  pagination via scroll offset.
- `/entity/:pointId` — entity page: typed header + lede, field table, Connections
  (list fields + edge vocabulary), Similar entries (vector recommend), raw-JSON
  drawer, external source link (from the overlay's `externalUrl` template),
  breadcrumb back-stack.

`Cmd-K` (or `Ctrl-K`) opens the command palette anywhere. `/` focuses the
landing search bar.

## Navigation model (important caveat)

Qdrant payloads contain **no explicit node-id graph edges**. So "links" are honest
about their mechanism:

- **Soft search links** — the inferred `subtitle` and `spatial` fields (`byArtist`,
  `area` in the music domain) and list-field items hold a *name string*. Clicking
  runs a name **search** — marked with a `⌕` icon and a "Search for …" tooltip. They
  are not hard graph edges.
- **Similar entries** come from Qdrant `recommend` (256-d cosine vector
  neighbors). Labelled "selected by meaning, not a direct link."
- The relationship **vocabulary** from the schema bundle is shown to *explain*
  what relationships a type participates in — but following one still runs a search.

We deliberately do not fabricate hard edges.

## Domain model

Everything schema-aware is funnelled through a single `Domain` object
([`src/lib/domain.ts`](src/lib/domain.ts)), loaded once at startup by
`DomainProvider` and read via `useDomain()`. It is built from two sources:

1. **An inferred role map** (`title` / `subtitle` / `tags` / `temporal` / `spatial` /
   `measures` / `relations` / `text`). [`src/lib/roles.ts`](src/lib/roles.ts) is a
   line-for-line TypeScript twin of the backend's `quickbeam/roles.py` —
   the same inference the server and `cdn bake` use. The map is taken from a baked
   domain manifest when present, otherwise inferred live from a sample of the
   collection. This is what lets the UI label and summarise *any* schema.
2. **An optional presentation overlay** — [`public/domain.json`](public/domain.json):
   per-type `icon`/`accent`/`singular`/`plural`/`definition`, `fieldLabels`
   overrides, and `externalUrl` templates (e.g. the MusicBrainz links). Pure polish:
   with no overlay, types get a hashed accent color, a first-letter badge, and
   `humanise()`d labels. `domain.json` also carries the bundle `nodes`/`edges` (the
   relationship vocabulary) and an optional `collection` name.

`public/domain.json` is the same shape `quickbeam cdn bake` writes into a domain's
`manifest.json`, so in the Tauri build a *pulled* domain becomes the `Domain`
directly. The current file was assembled from the bundle schema + the old
hardcoded tables; regenerate it by re-baking or editing it by hand.

> Note: a small amount of flavor microcopy still lives in
> [`src/lib/copy.ts`](src/lib/copy.ts) (e.g. the search placeholder). That's cosmetic,
> not structural — move it into the overlay later if you want it per-domain too.

## Qdrant endpoints used (via `/qdrant` proxy)

- `GET /collections/fangorn` — total points + vector size.
- `POST /collections/fangorn/points/count` — per-type / filtered counts.
- `POST /collections/fangorn/points/scroll` — browse / paginate.
- `GET  /collections/fangorn/points/{pointId}?with_payload=true` — one point.
- `POST /collections/fangorn/points/recommend` — semantic neighbors.

Routing uses the **Qdrant point id** (`result.points[].id`), which is distinct
from `payload.id` (the MusicBrainz mbid).
