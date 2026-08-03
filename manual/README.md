# Surge XT Manual — a knowledge-graph reader

A classic **user-manual reading experience** over the surgext knowledge graph
([`../surgext-source`](../surgext-source)), coupled with semantic search and the full
patch↔manual mesh. A sibling to [`../examples`](../examples) (the places/music explorer) that
keeps the exact same pattern — **static, fully in-browser** (Vite + React + TS): it downloads a
baked Semantic-CDN snapshot, embeds queries client-side with transformers.js, and searches with no
backend.

## What it does

- **Left TOC tree** — the manual's chapter/section hierarchy, built purely from each heading node's
  `breadcrumb` field (no edge file needed), ordered by page.
- **Reading pane** — section prose, breadcrumbs, child sections, a **parameter table**
  (name / description / range), and prev/next linear navigation.
- **Semantic search** (`/search?q=…`) — in-browser nomic embedding → cosine over the served
  vectors, grouped by entity type. If the model can't load it degrades to a **tokenized keyword
  match with a visible notice**, and a **"Fetch more"** control pages deeper into lower-relevance
  results.
- **The mesh** (via `lib/edges.ts`, the browser twin of quickbeam's MCP `neighbors`): a
  filter / effect / oscillator page shows **"Heard in N patches"** (browsable), and a Patch page
  links back to the filters / oscillators / effects it uses. Relationships are labelled by their
  real mechanism ("via usesFilter", "via semantic similarity") — no fabricated edges.
- **Figures** — each section's screenshots and rendered block diagrams show inline in the reading
  pane. To keep browsing private, all figures ship as **one bundle** (`images.json`) downloaded once
  at startup and served from in-memory `blob:` URLs, so opening a section makes **no per-figure
  network request** (the host sees one identical bundle fetch for every visitor). A from-chain
  deploy can instead resolve figures by their IPFS `cid` (see Config).

## Run it

```bash
npm install
npm run dev            # serves the staged snapshot from public/cdn
```

Home shows the chapter grid; the TOC and reading pane work offline (the shard is local under
`public/cdn`). The **first search** downloads the embedding model (transformers.js, cached after).

```bash
npm run build          # tsc -b && vite build  → dist/
npm run build:static   # stage the CDN into public/cdn, then build
```

## How it's built

Fully static and domain-driven, so the same shell renders any doc-shaped Semantic-CDN domain.

- `src/lib/data.ts` — downloads the manifest + gzipped NDJSON shards into memory once, serves
  `getPoint` / `search` (semantic + lexical fallback) / `recommend` / `pointsByType`. A condensed
  cousin of `examples/src/lib/shards.ts`.
- `src/lib/embed.ts` — in-browser query embedder (copied from examples): nomic-embed-text-v1.5,
  `search_query:` prefix, matryoshka to **256 dims** (must match the baked shard vector dim).
- `src/lib/edges.ts` — loads the typed linkset served at `/domains/<d>/edges` and walks it
  (`neighbors(id, rel, dir)`), powering the mesh.
- `src/lib/images.ts` — downloads the figure bundle (`/domains/<d>/images.json`) once and turns each
  image into an in-memory `blob:` URL, so figures render with no per-figure request.
- `src/lib/toc.ts` — builds the TOC tree + prev/next order from `breadcrumb` fields.
- `src/lib/corpus.tsx` — loads points + TOC + presentation once and shares via context.
- `src/pages/` — `Home`, `EntityView` (dispatches Section-vs-Patch rendering + all the rails),
  `SearchResults`. Entity routes are `/entity?id=<encoded>` (node ids contain `:` and `/`, so a
  query param rather than a path segment).

## Config

Env (all optional, sensible defaults):

- `VITE_CDN_URL` — where to fetch the snapshot. Default `/cdn` (the staged, same-origin copy). Set
  to `/cdn-live` to use the dev proxy to a running `quickbeam cdn serve`.
- `VITE_DOMAIN` — the domain name. Default `surgext`.
- `VITE_IPFS_GATEWAY` — gateway used to resolve a figure by its on-chain `cid` when the same-origin
  bundle isn't present (a from-chain deploy). Default `https://ipfs.io`. The bundle is preferred, so
  this only fires as a fallback — the private CDN deploy never hits IPFS.

## Regenerating the data

The app reads a baked CDN snapshot produced from the surgext graph. The build spec + intermediate
output live in [`../manual-build`](../manual-build); regenerate with the commands documented there,
then `npm run stage:cdn -- --domain surgext` copies it into `public/cdn`.

> The one constraint: bake at **256 dims** — `embed.ts` truncates queries to 256, so the served
> document vectors must be 256 too. See `../manual-build/README.md`.

## Deploy (Cloudflare Pages)

The site is fully static and self-contained — `npm run build:static` bundles the app **and** the
baked CDN snapshot into `dist/`, so there's no backend to host. `public/_redirects` provides the
SPA fallback Pages needs for the `/entity` and `/search` routes.

Deploy with Wrangler direct-upload (build locally, then upload — the CDN snapshot is baked locally
against Qdrant, so CI/Git-integration can't regenerate it):

```bash
npm install
npm run build:static                                    # -> dist/ (app + CDN snapshot)
npx wrangler@latest login                               # once
npx wrangler@latest pages deploy dist --project-name surgext-manual
```

(Use your actual project name from the wrangler output — the live deploy is `surgext-manual-54z`.)
Then attach the custom domain in the Cloudflare dashboard (Workers & Pages → the project →
**Custom domains**, not a hand-made DNS CNAME — that yields a 522) → `surgext-manual.fangorn.network`.
Since `fangorn.network` is a zone on the same account, Pages creates the CNAME + TLS cert
automatically. Re-deploy by re-running the last two commands (regenerate the data first per
`../manual-build/README.md` if the graph changed), then **purge the Cloudflare cache** so the new
`index.html` is served.

Notes: browsing is served entirely from the site; **search** downloads the embedding model from the
HuggingFace CDN in the visitor's browser on first use. The bundled ONNX WASM (~23 MB) is under
Pages' 25 MiB/file limit.
