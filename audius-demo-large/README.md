# audius-demo-large

The Audius pitch at full scale: **the whole public catalogue — 1.4M tracks — searchable
from a browser, with your query never leaving the tab.**

This is the large-corpus sibling of [`audius-demo`](../audius-demo). That one downloads
its entire 25k-record snapshot and searches it locally, which is why its search is
perfectly private and why it cannot grow. This one keeps the first property and drops
the second.

## How this version works

Two domains, one id space.

| | `audius-home` | `audius-large` |
|---|---|---|
| what it is | a bootstrap slice — 41,424 records | the whole build — 1,904,468 records |
| how it arrives | downloaded **whole** at page load (~124 MB) | never; reached one bucket at a time |
| what it carries | records + vectors + edges + a precomputed shell | vectors in Qdrant, plus a public codebook |
| what it discloses | **nothing** — identical bytes for every visitor | one integer per uncached query |

Both are cut from a single build of Audius' published Postgres dump, so a record that
appears in both has the same id, the same fields and the same vector. That is what lets
a search hit dedupe against the resident copy instead of showing you the same track
twice.

**The landing page is unchanged in character.** The hero ledger, onboarding, the
publisher grids and every relation rail render from the bootstrap alone, before any
search touches the network. Nothing about that first minute is observable.

**Search reaches the rest.** Your query is embedded in this tab, routed in this tab
against a *codebook* you downloaded, and only a *bucket* number is sent. Candidates come
back with their vectors and are re-ranked here against your real query. (If "codebook"
and "bucket" mean nothing to you yet, [the next section defines
them](#first-the-four-words-this-rests-on) from scratch — they are the whole mechanism.)

**Records outside the bootstrap are first-class.** Click a search result and it opens
with real relation rails, plays, and feeds the recommender — served by
`GET /adjacency` and `GET /records` rather than a resident lookup. Before that wiring
existed, a search hit opened to "Not in this snapshot", which is the failure this
architecture most obviously invites.

## Privacy, and what a bucket is

The design goal is narrow and worth stating precisely: **we cannot tell what you
searched for, and we should not be able to even if you do trust us.**

### First, the four words this rests on

If you have not worked with embeddings, the rest of this section is unreadable without
these. The first three are standard terms from vector search; only the fourth is ours.

**A vector (or embedding)** is what a piece of text becomes when a model reads it: a
fixed-length list of numbers, 256 of them here. The useful property is that *similar
meanings produce similar numbers*. "melancholy piano" and "sad solo keys" land close
together; "death metal" lands far away. "Close" is literal — you can measure the
distance between two of these lists arithmetically. Every track in the catalogue was
turned into one of these at build time, from its title, artist, genre, mood and tags.
Searching means turning your words into a vector and finding the nearest track vectors.

**A cell** is a cluster of nearby vectors. At build time we ran k-means — a standard
clustering algorithm — over all 1.9M track vectors and told it to produce **3,719
groups**. Each group is a cell, and each cell has a *centroid*: the average position of
everything in it, one more 256-number vector that serves as that cell's address.
Because clustering puts similar things together, a cell ends up being a coherent slice
of the catalogue: one might be mostly deep house, another mostly acoustic folk.

**The codebook** is the list of all 3,719 of those centroids — nothing more than an
array of 3,719 × 256 numbers, shipped as a **952 KB file** (`index/codebook.i8`) that
your browser downloads once and keeps.

It exists so you can work out which cell your query belongs to *without asking anyone*.
Finding your cell means comparing your query vector against 3,719 centroids and taking
the nearest — arithmetic over a file you already hold, no network involved. That is the
step that would otherwise have to happen on a server, and having it happen in your tab
is the difference between sending your query and sending a number.

The name is borrowed from vector quantisation, where a codebook is the fixed set of
reference values you round a signal to. Same idea here: your query gets rounded to the
nearest of 3,719 reference points, and only that choice is expressed.

**It is public on purpose, and that is not a compromise.** Every visitor downloads the
same codebook, and anyone can fetch it and read it. The privacy of the scheme never
rested on it being secret — it rests on the *bucket* below, and on the routing
happening locally. A secret codebook would be worse: you could not verify what your
browser was routing against.

**A bucket** is our addition: a fixed group of **8 cells that are deliberately not
related to each other**. The 3,719 cells are shuffled and dealt out like cards into 464
buckets, so one bucket might hold a deep-house cell, a gospel cell, a K-pop cell and
five others with nothing in common. Which cells go in which bucket is fixed, public,
and the same for every visitor.

### What actually happens when you search

An embedding is not an opaque token — published work reconstructs short texts from
their vectors near-exactly, so "we only send the vector, not the words" is not a
privacy claim at all. Sending a *perturbed* vector fails too: the server holds the
corpus, so it can denoise, and per-query noise averages out over a session.

So nothing continuous is sent. Instead, for a real query — *"melancholy piano for a
rainy night"*:

1. **Your words become a vector, in your tab.** The embedding model runs in a Web
   Worker in your browser. Your text never goes anywhere.
2. **That vector is compared against the 3,719 centroids, in your tab.** The nearest
   one wins — say **cell 2702**. This is just 3,719 distance calculations against a file
   you already downloaded; no network involved. Nothing about this step is observable.
3. **You look up which bucket cell 2702 belongs to** — say **bucket 154** — and that is
   what you ask for. `GET /bucket/154`. One integer, no query string, no body.
4. **The server sends back everything in all 8 of that bucket's cells** — roughly 4,000
   tracks. It has no way to know which of the eight you were after, because you did not
   say, and the seven others are unrelated to the one you wanted.
5. **Your browser ranks those 4,000 against your real query vector** and shows you the
   best ten. The precise ranking happens where your query has always been: in the tab.

The server's whole view of that search is the number **154**.

**The expansion is server-side, and that matters.** `GET /bucket/{id}` accepts an
integer and nothing else — there is no parameter for "just cell 2702". A client cannot
opt out of its own anonymity set even if it wanted to, because the route cannot express
it.

### Why buckets are scattered rather than coarse

The eight cells in a bucket are **deliberately unrelated** — assigned by a shuffled
round-robin, not by geography. This is the part that does the work.

The obvious alternative is to make the cells bigger instead — cluster into 464 large
groups rather than 3,719 small ones, and ask for one of those. **That does not work,
and understanding why is the point of the design.**

A cell is a cluster of *nearby* vectors, and nearby vectors mean similar music. So a
big cell is still one coherent region — a large slice of "electronic", say. Naming it
tells the server roughly what you were looking for, no matter how large you make it.
Size is not the variable that hides you; *coherence* is.

A scattered bucket breaks the coherence directly. Its eight cells are unrelated by
construction, so the set as a whole describes no particular kind of music, and naming it
says little. Measured on this build — all 1,391,364 records carrying a genre, against
the codebook actually shipped:

| what is disclosed | how well it predicts the record's genre |
|---|---|
| baseline (guess the most common genre) | 0.178 |
| **its cell** | **0.802** |
| **its bucket** (8 scattered cells) | **0.337** |

A cell almost gives your genre away. A bucket gets most of the way back to knowing
nothing — a roughly fivefold reduction in what a single request reveals.

It is not zero, and the honest reading is that a bucket still narrows your genre
somewhat. Reproduce it from `index/cells.ndjson.gz` (which record is in which cell) and
`index/layout.json` (which cell is in which bucket) — and if the codebook is ever refit
with a different number of cells or a different bucket size, **re-measure**. These are
properties of one specific fit, not guarantees of the design.

The practical consequence is that the two things you would want to tune are
independent: **how many cells** controls retrieval quality (finer cells, better
matches), and **how many cells per bucket** controls privacy (more cells, less
disclosed, more bytes). Neither knob fights the other.

### What this does not hide

Three things, stated plainly because the claim survives them and overstating it is the
fastest way to lose the room:

- **A themed session narrows things.** Buckets are a deterministic function of your
  query, which is what stops per-query noise from averaging out — but it also means a
  server can compare several of your requests. Measured over 40 trials, four related
  searches narrow the candidate set by roughly **3–7×**. That is far from identifying
  your query and it is not nothing. Repeat searches in an area you have already fetched
  send **nothing at all**, because buckets are cached.
  *(That range was measured on a 500k subset at `k=977` with the same bucket size of 8,
  not re-run against this 1.9M codebook. The mechanism and bucket size are identical so
  it should carry — but re-measure before quoting it as a guarantee.)*
- **Browsing is not private and never was.** Opening a record asks for that record and
  its edges, and its artwork loads from Audius' own content nodes. The pages you *open*
  are visible in a way the things you *search for* are not. `audius-demo` has always
  had the artwork property; this build adds the record and adjacency fetches.
- **Audio streams from a content node** when you press play, as in the smaller demo.

### How the claim is enforced, not just asserted

All network access in the retrieval path lives in exactly one file,
[`src/lib/store.ts`](src/lib/store.ts), so the claim has a single place to be audited.
`npm run check:private` **executes the falsifier as a test**: it records every request a
real query makes and asserts each URL is a bare `/bucket/{n}` with an empty body — no
vector, no query string, no per-client random value. If someone later reintroduces one,
that check fails rather than the property quietly disappearing.

## Run it

Four processes. See [`../audius-large-build/RUNBOOK.md`](../audius-large-build/RUNBOOK.md)
for the full version, including what to verify in a browser.

```sh
cd ..                                    # quickbeam/
docker start qdrant                      # 1.9M embedded records

venv/bin/python -m quickbeam.cli cdn serve \
    --cdn-dir ./audius-large-build/cdn --cors --port 8092

venv/bin/python -m quickbeam.cli serve \
    --collection audius-large --port 8081 \
    --index-layout ./audius-large-build/cdn/audius-large/index/layout.json \
    --adjacency-db ./audius-large-build/stage/edges.sqlite

cd audius-demo-large && npm run dev       # http://localhost:5181
```

Port **5181**, so `audius-demo` on 5180 can run beside it — the easiest way to see the
difference is both open at once.

`/cdn` and `/api` are proxied through the app's own origin (`vite.config.ts`). Naming
them absolutely works on this machine and nowhere else: through a tunnel `localhost` is
the *visitor's* machine, and an `http://` call from an `https://` page is blocked as
mixed content anyway. **Those proxies exist only in the dev server** — a static `dist/`
has no equivalent, so a production deploy needs both origins reachable another way.

### Checks

```sh
npm run check:graph     # 19 checks over the bootstrap graph
npm run check:private    # 11 checks incl. the executed falsifier
npm run check:remote     # the non-resident record path, end to end
npm run private-search -- "your query"   # human-readable single query
```

`check:remote` is the one that matters most for this build: it searches for something
the bootstrap cannot answer, then asserts the hit opens, has rails, is playable, **and
that the kernel learns from it**. That last one is the sharpest silent failure here —
`kernelSignal` no-ops when it cannot find a vector, so playback looks perfectly normal
while the recommender goes deaf.

## Why this exists instead of `examples/`

`examples/` re-ranks search results with a local Qwen2.5-0.5B text-generation model
(`src/lib/llm.ts`). transformers.js decodes that on the **main thread**, so the UI
freezes mid-search — and `requestIdleCallback` doesn't help, because once WASM
decoding starts the thread is gone until it finishes.

This app is built so that can't happen:

- **No in-browser LLM and no re-ranking.** Nothing in this pitch needs one.
- **Everything expensive lives in a Web Worker** (`src/worker/search.worker.ts`): the
  bootstrap graph, the vector block, the embedding model, the codebook and the edge
  adjacency. The main thread renders and does nothing else.

The spinner during a search is a plain CSS animation, so it doubles as a canary: if it
ever stops turning, something has escaped onto the main thread.

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
npm run check     # needs Qdrant up with the `audius-large` collection
```

embeds fixed queries through the app's own code and asserts the top hits match what
Qdrant returns for the same vector.

## Privacy, stated accurately

See **[Privacy, and what a bucket is](#privacy-and-what-a-bucket-is)** above for the
full account — including the measured numbers, and the three things this design does
not hide. The short version:

- **What you search for** is not observable. Your query is embedded and routed in this
  tab; a bucket number is the only thing that leaves it.
- **What you open** is observable. Records, their edges and their artwork are all
  fetched as you browse.

That distinction is the whole design. `src/views/Privacy.tsx` states it in the user's
words and names, in a header comment, exactly what would falsify it.
