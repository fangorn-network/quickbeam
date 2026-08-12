# audius-demo-large — running it locally

Four processes. Everything the browser touches rides the app's own origin through
vite's proxy, so one tunnel covers the whole demo and there is no CORS or mixed
content to fight (`vite.config.ts`).

```bash
cd /home/coleman/fangorn/quickbeam

# 1. Qdrant — holds all 1,904,468 embedded records
docker start qdrant

# 2. The CDN: bootstrap shards + the public codebook
venv/bin/python -m quickbeam.cli cdn serve \
    --cdn-dir ./audius-large-build/cdn --cors --port 8092

# 3. The private-retrieval API: bucket fetches + adjacency + records
venv/bin/python -m quickbeam.cli serve \
    --collection audius-large --port 8081 \
    --index-layout ./audius-large-build/cdn/audius-large/index/layout.json \
    --adjacency-db ./audius-large-build/stage/edges.sqlite

# 4. The app  ->  http://localhost:5181
cd audius-demo-large && npm run dev
```

`audius-demo` still runs on **5180**, so both can be open side by side — which is the
easiest way to see what changed: same UI, one searching 25k locally, the other
searching 1.4M through a bucket id.

## What to expect

- **First load downloads ~124 MB** (9 shards + a 3.6 MB gzipped linkset). That is the
  bootstrap graph, and it is the *only* thing that arrives whole. It is cached, so a
  reload is fast.
- Onboarding needs **≥3 genres and exactly 3 artists** — 52 genres and 2,157 artists
  qualify, so it is satisfiable. If it ever is not, the app has no way past that
  screen; `bootstrap.py` asserts it at build time for exactly that reason.
- The first **search** downloads the ONNX model (~20 MB, cached by transformers.js)
  before it can embed. Subsequent searches are instant.

## What to actually check in the browser

The node checks drive `Graph` directly and cannot see React, layout, or the network
tab. These are the things only a browser can tell you:

1. **Home** — the two-publisher ledger renders, both grids fill, the kernel rail
   appears after onboarding.
2. **Search something obscure** ("gregorian chant", "bagpipe drone", "harpsichord").
   Results should come back from outside the bootstrap. Open DevTools ▸ Network and
   confirm the only query-dependent request is `GET /api/bucket/{n}` — no query
   string, no vector, no body. That is the entire privacy claim, visible.
3. **Click a search result.** It must open a real page with real rails — not
   "Not in this snapshot". That is what Stage 4 exists for.
4. **Press play on it**, then check "Where you're heading" changes. If playback works
   but the rail never moves, the kernel is not learning from non-resident records —
   the sharpest silent failure in the design (`check:remote` pins it).
5. **Search the same thing twice.** The second search should make NO bucket request
   (`store.ts` caches by bucket id). That is what stops disclosure accumulating.
6. **Open a Genre or Tag page.** These have hundreds of thousands of inbound tracks;
   "Show more" pages by 48 rather than rendering them all.

## Serving the built site instead of the dev server

`npm run build` emits a static `dist/`, but the dev server's `/cdn` and `/api` proxies
do NOT exist in production — `vite.config.ts` only configures the dev server. A static
deploy needs both origins reachable some other way (a reverse proxy, or absolute
`VITE_*` URLs over https). Verify a production build behind a real proxy before
deploying; `python3 -m http.server` over `dist/` will 404 every `/cdn` and `/api` call.
