# audius-demo

The browser front end for the Audius pitch: **two independent publishers, one
searchable music graph**, styled to Audius and running entirely client-side.

## Why this exists instead of `examples/`

`examples/` re-ranks search results with a local Qwen2.5-0.5B text-generation model
(`src/lib/llm.ts`). transformers.js decodes that on the **main thread**, so the UI
freezes mid-search — and `requestIdleCallback` doesn't help, because once WASM
decoding starts the thread is gone until it finishes.

This app is built so that can't happen:

- **No in-browser LLM and no re-ranking.** Nothing in this pitch needs one.
- **Everything expensive lives in a Web Worker** (`src/worker/search.worker.ts`): the
  65 MB snapshot, the 25k×256 vector block, the embedding model, and the edge
  adjacency. The main thread renders and does nothing else.

The spinner during a search is a plain CSS animation, so it doubles as a canary: if it
ever stops turning, something has escaped onto the main thread.

## Run it

```sh
# 1. serve the baked snapshot (from quickbeam/)
venv/bin/python -m quickbeam.cli cdn serve --cdn-dir ./audius-build/cdn --cors --port 8090

# 2. the app
cd audius-demo && npm install && npm run dev      # http://localhost:5180
```

### Showing it on another device (ngrok, Cloudflare tunnel, a phone)

Tunnel **only the app port**. The snapshot rides along with it:

```sh
ngrok http 5180
```

The dev server proxies `/cdn` → `http://localhost:8090`, so the browser fetches the
snapshot from the same origin it loaded the page from. Nothing else to expose.

Do **not** set `VITE_CDN_URL` to `http://localhost:8090` for this. That address means
"this device" — on a phone it resolves to the phone, which is the `NetworkError when
attempting to fetch resource` you get. And because tunnels serve https, an http://
fetch from the page would be blocked as mixed content even if the host were right.

If `cdn serve` runs somewhere other than `localhost:8090`, point the *proxy* at it —
`CDN_TARGET=http://host:port npm run dev` — not the browser.

| Env var | Default | Purpose |
|---|---|---|
| `VITE_CDN_URL` | `/cdn` (same origin, proxied) | Override only if the CDN is genuinely served elsewhere over https |
| `CDN_TARGET` | `http://localhost:8090` | Where the dev server's `/cdn` proxy forwards to |
| `VITE_DOMAIN` | `audius` | Baked domain name |
| `VITE_CONTENT_NODE` | `https://creatornode.audius.co` | Resolves artwork CIDs |
| `VITE_DISCOVERY_NODE` | `https://discoveryprovider.audius.co` | Resolves `/stream` for playback |
| `VITE_PLATFORM_OWNER` | `0x1111…1111` | Which wallet is the platform; the other is the artist |

First load pulls the whole snapshot once (progress is real, streamed from
`content-length`). The first search additionally downloads the embedding model from
the HuggingFace CDN. After that, searching is offline.

## Design

Tokens are lifted from Audius' own open-source **Harmony** design system
(`AudiusProject/audius-protocol`, `packages/harmony/src/foundations`) rather than
eyeballed — background `#202131`, surface `#2f3348`, primary `#d767e1`, secondary
`#c67cff`, gradient `linear-gradient(315deg, #5b23e1, #a22feb)`, and their corner-radius
and type scales. Avenir Next LT Pro is Audius' face but proprietary, so **Figtree** sits
next in the stack.

It is deliberately badged **Fangorn** in the header. It should feel native to Audius'
world without presenting as an Audius property.

**The signature element** is the root ledger on the home page: two on-chain roots with
the linkset drawn between them as one strand per relation type, thickness proportional
to its real count. Every number is read from the served snapshot, so the diagram can't
drift from what it illustrates. The same marker reappears in miniature on any relation
rail that crosses publishers — which is the claim, demonstrated on real records.

Publishers are distinguished by **shape as well as hue** (filled chip vs outlined chip
with a dot), so the split survives a colour-blind viewer and a bad projector.

## Playback

Tracks play. `fields.id` is the Audius track id, so the stream URL is
`{discovery}/v1/tracks/{id}/stream?app_name=…`, which 302s to a content node serving
`audio/mpeg` with byte ranges and open CORS — everything a plain `<audio>` needs, with
no data change and no re-embed. Audius' own redirect appends `skip_play_count=true`,
so **demoing this does not inflate anyone's play counts**.

There is exactly **one** `<audio>` element for the whole app, owned by
`src/lib/player.tsx` and mounted above `App` so navigating between views doesn't stop
the music. The usual failure here is an element per card; there isn't one. Playback is
handled by the browser's media pipeline rather than JS, so it costs the main thread
nothing and the worker architecture is unaffected.

`play(track, queue)` takes the list the click came from, so next/prev work inside a
result grid, a relation rail, or a playlist without any queue UI. The now-playing bar
carries the **publisher chip**, so whatever is playing states which wallet published
it — the argument running continuously while the room looks elsewhere.

About **1% of tracks are stream-gated or withdrawn** (measured: 101 of 11,112). Those
report "unavailable" rather than failing silently; `npm run check:stream` re-measures
that against live data.

## The session kernel

`src/kernel/` is **sonder's** Markov kernel
(`sonder/app/src/renderer/src/kernel/`), ported close to verbatim so the two stay
diffable. It keeps a position μ and velocity v over the embedding space, plus taste
accumulators, artist affinity, a skip-repulsion region, and entropy-coupled Gibbs
sampling. Plays, skips and explicit ♥/– all feed the same two transitions — a thumb
is just unambiguous evidence of the same kind, so there is one state machine, not two.

It runs **in the worker**, next to the vectors, and the main thread holds only a
snapshot. `quickbeam/examples/src/lib/sessionKernel.ts` is a simplification of the
same thing and was used only as a reading aid.

Three adaptations, all in `src/kernel/adapt.ts` and `constants.ts`:

| | sonder | here |
|---|---|---|
| Dimension | 384 (all-MiniLM) | **256** (nomic + matryoshka) |
| Distance | Chroma squared-L2 | **`2 − 2·cos`** — exact, both sides unit-norm |
| Taxonomy | genres/moods/themes/contexts | genre / mood / tags; **`contexts` left empty** |

`contexts` stays empty on purpose. `dimScore` returns its floor for an empty
dimension, so an unused one is neutral — whereas the tempting filler, publisher,
would teach the kernel to prefer one wallet, which is the single bias this demo
cannot have.

The parameters in `constants.ts` are sonder's, calibrated against a 384-d cloud.
They are a starting point here, not a fitted answer; `lambda_max`, `sigma_base` and
`gamma_reg` are the knobs if the rail feels too tight or too jumpy.

### Why there are two rails

Measured, not assumed: seed the kernel with three Disclosure tracks and the top ~20
recommendations are *all* Disclosure — `tau_art` correctly favours the artist you just
played, and they have ~38 unheard tracks. Crossing to the platform only begins around
k=40 (15/40, then 34/60).

That is a good recommender and a poor demonstrator. So the second rail runs the
**same ranking narrowed to the other publisher** — filtered, never re-ranked. If the
model ranks nothing over there, the rail is simply absent. Asserting a cross at k=12
would have meant asserting a worse recommender.

## The one thing that can break silently

`src/lib/matryoshka.ts` re-implements the query-side vector transform that
`quickbeam/ingest/identity.py` applied to the documents: layer-norm over all 768 dims →
slice to 256 → L2-normalize, with the asymmetric `search_query: ` / `search_document: `
prefixes nomic-embed expects. If those drift apart nothing throws — ranking just
quietly degrades.

```sh
npm run check     # needs Qdrant up with the `audius` collection
```

embeds fixed queries through the app's own code and asserts the top hits match what
Qdrant returns for the same vector.

## Privacy, stated accurately

The claim this demo makes is specific: **queries never leave the browser**. The
searchable snapshot is downloaded once and searched locally, so no server sees what you
typed or what you were looking for — which is the part a discovery-node operator
actually can't offer today.

It is not a claim that nothing touches the network. Two things do:

- **Artwork** is fetched by CID from a content node as you browse.
- **Audio** streams from a content node when you press play.

Both are ordinary content-addressed retrieval of blobs the snapshot deliberately
doesn't carry, and both are visible to whichever node serves them. Say this plainly
rather than letting a CTO find it — the search-privacy claim survives it intact, and
overstating the claim is the fastest way to lose the room. If the browsing trace ever
needs to be private too, the fix is the one the surgext demo used: bundle the assets
into the snapshot.
