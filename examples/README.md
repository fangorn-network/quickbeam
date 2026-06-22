# Schema Browser

A wiki-style browser for the Fangorn `fangorn` Qdrant collection (MusicBrainz
entities embedded as 256-d vectors). Dark, information-dense, keyboard-first.
React + Vite + TypeScript + plain CSS Modules. No UI framework.

## Prerequisites

- **Node 18+** (developed on Node 22).
- **Qdrant running at `http://localhost:6333`** with a collection named
  `fangorn`. The app talks to Qdrant through a Vite dev proxy
  (`/qdrant/*` → `http://localhost:6333/*`), so no CORS config is needed.

The app is **resilient when Qdrant is unreachable**: the TopBar shows a
connection-error badge and content areas render inline error states instead of
crashing.

## Run

```bash
npm install
npm run dev      # http://localhost:5173
npm run build    # tsc -b + vite build (must pass cleanly)
npm run preview  # serve the production build
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
  drawer, external MusicBrainz link, breadcrumb back-stack.

`Cmd-K` (or `Ctrl-K`) opens the command palette anywhere. `/` focuses the
landing search bar.

## Navigation model (important caveat)

Qdrant payloads contain **no explicit node-id graph edges**. So "links" are honest
about their mechanism:

- **Soft search links** (`byArtist`, `area`, list-field items) hold a *name string*.
  Clicking runs a name **search** — marked with a `⌕` icon and a "Search for …"
  tooltip. They are not hard graph edges.
- **Similar entries** come from Qdrant `recommend` (256-d cosine vector
  neighbors). Labelled "selected by meaning, not a direct link."
- The relationship **vocabulary** from the schema bundle is shown to *explain*
  what relationships a type participates in — but following one still runs a search.

We deliberately do not fabricate hard edges.

## Schemas snapshot

The JSON schemas in `public/schemas/` are a **snapshot** copied from the source
of truth. They are loaded at runtime via `fetch('/schemas/...')` to render typed
fields (`fangorn.mb.<type>.v3.json`) and the relationship edge vocabulary
(`fangorn.mb.creativecore.v3.json`, `bundle.edges`).

To re-copy the latest snapshot:

```bash
cp /home/driemworks/fangorn/embeddings/stage_volumes/schemas/*.json public/schemas/
```

## Qdrant endpoints used (via `/qdrant` proxy)

- `GET /collections/fangorn` — total points + vector size.
- `POST /collections/fangorn/points/count` — per-type / filtered counts.
- `POST /collections/fangorn/points/scroll` — browse / paginate.
- `GET  /collections/fangorn/points/{pointId}?with_payload=true` — one point.
- `POST /collections/fangorn/points/recommend` — semantic neighbors.

Routing uses the **Qdrant point id** (`result.points[].id`), which is distinct
from `payload.id` (the MusicBrainz mbid).
