# Discovery without custody

**A working demo of Audius' catalog split across two sovereign publishers, searched as
one graph — by a person in a browser and by an agent over MCP, with no server seeing
either query.**

---

## The claim

Discovery today requires custody. To rank an artist's track, a platform must hold that
artist's data inside its own index. That is true of Audius, and it is true of every
competitor — it is the reason a "decentralized" music network still has a centralized
discovery layer.

This demo takes that assumption apart and shows the system still works without it:

- The **platform's graph** is published by one wallet, with one artist's catalog
  **removed** — a hole where Disclosure should be, holding only a stub that says "this
  artist exists."
- The **artist's graph** is published by a different wallet: their own catalog, profile,
  albums and playlists.
- A **linkset** — 113 edges, authored by neither party — joins them.

Search spans both. Traversal crosses between them. Neither publisher can read, revoke, or
rank the other's records, and no index sits in the middle. That is the whole argument, and
it runs today on real Audius data.

---

## Status: what is real right now

A pitch is worth nothing if the demo's limits surface during the demo. Stated up front:

| Piece | Status |
|---|---|
| Two-publisher split of real Audius data | **Running.** 46,260 crawled records → 25,372 embedded, 107,867 edges |
| The linkset joining them | **Running.** 113 cross-publisher edges, 0 unresolved endpoints |
| Browser client, client-side search | **Running.** `audius-demo`, 65.7 MB snapshot, no query leaves the tab |
| Agent client over MCP | **Running.** 9 tools, semantic + typed traversal |
| Fangorn contracts on Arbitrum Sepolia | **Deployed.** DataRegistry + SubscriptionRegistry, both fees currently 0 |
| **This build settled on-chain** | **Not yet.** Every record carries `meta.manifestCid = "local-prebake"`, and the two publishers are placeholder wallets `0x1111…` / `0x2222…` |

The last row matters and is deliberate. The publish path exists and is documented
(`audius-build/RUNBOOK.md`, Part 2): the same pipeline run with `--publish` pushes each
side to its own on-chain root under its own wallet. It needs two funded, registered
wallets and a real Pinata key. Until that is run, the demo proves **the architecture**,
not the settlement. Everything downstream of the roots — the split, the linkset, both
clients — is unaffected by it, because they read the baked snapshot either way.

---

## How it hooks together

```
 Audius API                                                     ┌── browser (a person)
     │  crawl once → cache (the determinism boundary)           │
     ▼                                                          │
 build_graph ──┬── side A: platform minus the artist  ─┐        │
               └── side B: the artist's own catalog   ─┤        │
                                                       │        │
 audius-link ───── linkset (113 cross edges) ──────────┤        │
                                                       ▼        │
                          embed (nomic, 256-d) → Qdrant         │
                                       │                        │
                          cdn bake → immutable shards ──────────┤
                          cdn edges → linkset          serve    │
                                                       :8090 ───┤
                                                                └── quickbeam mcp (an agent)

           settlement (Fangorn):  each side's graph → its own bytes32 root on Arbitrum
```

### 1. One crawl, two graphs

Audius is live — play counts tick, trending reorders every few minutes. So the crawl
writes a raw record cache **once**, and every graph build reads only from that cache.
Same cache → same content → same content-addressed ids → same commit. A flaky discovery
node can't break a demo that's already been crawled, and re-crawling becomes a deliberate
act that produces a reviewable diff.

Both sides come out of **the same `build_graph` function**, so they are schema-identical
by construction. A separate "artist source" package would have guaranteed drift, and drift
is exactly the failure a federation argument cannot afford.

The split is a partition, not a copy: `audius-source/tests/` asserts that side A + side B +
the linkset account for **every edge** the undivided graph had. Splitting a graph across
two sovereign publishers destroys nothing.

### 2. The linkset is data, not a build step

The interesting edges are the ones neither publisher could author alone:

| Relation | Count | What it is |
|---|---|---|
| `contains` | 55 | A platform playlist holding the artist's tracks |
| `remixOf` | 52 | A platform track remixing the artist's, or vice versa |
| `favorited` | 4 | Cross-publisher favorites |
| `reposted` | 1 | |
| `sameAs` | 1 | The platform's stub ≡ the artist's real node |

The platform knows Disclosure exists; it does not hold their catalog. The artist holds
their catalog; they don't control the platform's playlists. The join is published as its
own artifact that anyone can inspect and recompute — **not** a merge either side performs
inside its own database. That distinction is the product.

### 3. Content addressing does a schema registry's job

Both publishers derive `Genre: Electronic` independently from their own tracks. The
payloads are byte-identical, so they hash to the same id, so the two graphs **merge at
that vertex** with no linkset entry and no coordination. 25,405 records staged, 25,372
delivered — **33 shared vocabulary nodes converged for free.**

This is the part worth dwelling on with an engineering audience: interoperability that
usually requires a governed schema registry falls out of content addressing instead.

One consequence that must be handled correctly: because a shared vocabulary node
content-addresses to *one* record carrying *one* owner, every edge into it looks like it
crosses publishers. Counting naively reports **12,657** crossings against this graph; the
true figure is **113**. Both clients exclude vocabulary nodes from that test. It's a small
detail that, gotten wrong, would have made the central number of the pitch a lie.

### 4. Settlement — the part that makes it sovereign

Fangorn is "git for knowledge graphs, settled on-chain." Each publisher versions their
graph off-chain as content-addressed IPLD blocks and anchors **one 32-byte pointer** — the
digest of their latest commit — on an Arbitrum contract.

- `commit` builds the blocks and uploads the delta. No transaction. Like `git commit`.
- `push` compare-and-swaps the on-chain root. Fast-forward only. Like a permissioned
  `git push`.
- The contract stores one root **per publisher**, emits `StateCommitted`, and enforces a
  linear timeline. It stores nothing else — no schema, no index, no records.

Anyone can read or subscribe with **an RPC node and a public IPFS gateway**. No indexer,
no subgraph, no API key. A consumer watches `StateCommitted`, diffs the new root against
the old, and gets the net added/removed records for a namespace — which is exactly what an
index or embedding builder needs, and it stays correct across a force-push.

For this demo, two roots means two wallets means two sovereign timelines. One wallet with
two namespaces would share a single root, and an engineer in the room would notice.

**Publisher onboarding — the account website** (`websites/fangorn`, separate from the demo
app). A publisher registers once on the DataRegistry, then subscribes for storage. The
site handles wallet (via Privy), a testnet faucet drip, registration, and the USDC
subscription, and shows storage usage against both the free tier and the daily cap. The
upload path itself never sees a Pinata key: a Cloudflare Worker verifies a signed
ownership challenge, checks one `access(address)` view on-chain, and mints a single-use
presigned upload URL scoped to that wallet's own group. Registration and subscription come
back in that one call, so the gate is one `eth_call` deep. Both fees are currently 0 on
testnet — the plumbing is real, the price is not yet.

---

## The website — what a person sees

`audius-demo/` — a Vite/React app styled with tokens lifted from Audius' own open-source
Harmony design system, deliberately badged **Fangorn** so it reads as native to Audius'
world without presenting as an Audius property.

### The privacy claim, stated exactly

**Queries never leave the browser.** The snapshot is downloaded once and searched locally,
so no server learns what you typed or what you were looking for. That is the thing a
discovery-node operator genuinely cannot offer today.

It is *not* a claim that nothing touches the network. Two things do: **artwork** is fetched
by CID from a content node as you browse, and **audio** streams from a content node when
you press play. Both are ordinary content-addressed retrieval of blobs the snapshot
deliberately doesn't carry. Say this before a CTO finds it — the search-privacy claim
survives it intact, and overstating it is the fastest way to lose the room.

### Architecture, and why it's built this way

Everything expensive lives in a **Web Worker**: the 65.7 MB snapshot, the 25,372×256
vector block, the embedding model, and the edge adjacency. The main thread renders and does
nothing else. The spinner is a plain CSS animation, so it doubles as a canary — if it ever
stops turning, something has escaped onto the main thread.

There is no in-browser LLM and no re-ranking. An earlier version re-ranked with a local
0.5B model; transformers.js decodes on the main thread, and the UI froze mid-search. The
pitch doesn't need one.

### What's on screen

- **The root ledger** on the home page: two publishers with their record counts, and the
  linkset drawn between them as one strand per relation type, thickness proportional to
  its real count. Every number is read from the served snapshot, so the diagram cannot
  drift from what it illustrates.
- **Publisher badges** on every card, distinguished by **shape as well as hue** — so the
  split survives a colour-blind viewer and a bad projector.
- **Playback.** Tracks play, via one `<audio>` element for the whole app, so navigating
  doesn't stop the music. Audius' own redirect appends `skip_play_count=true`, so demoing
  this does not inflate anyone's play counts. About 1% of tracks are stream-gated or
  withdrawn (measured: 101 of 11,112); those report "unavailable" rather than failing
  silently.
- **A session kernel** — a Markov model over the embedding space (position, velocity, taste
  accumulators, skip repulsion) that drives recommendations from what you play and skip. It
  runs in the worker, next to the vectors.

One honest note on the recommender: seeded with three Disclosure tracks, the top ~20
recommendations are *all* Disclosure — correct behaviour, poor demonstration. So a second
rail runs the **same ranking narrowed to the other publisher**, filtered, never re-ranked.
If the model ranks nothing over there, the rail is simply absent. Asserting a cross that
the model didn't make would have meant shipping a worse recommender to win an argument.

---

## MCP — what an agent sees

The browser is one client of the served snapshot. `quickbeam mcp` is another: an MCP
server that pulls the same shards and the same linkset and exposes them as agent tools.
It is a **pull-client, not a proxy** — the data comes to the process, and the query stays
there. Same privacy property as the browser, offered to an agent.

This matters commercially: it is the difference between "Audius has an API an agent can
call" and "an agent can hold Audius' catalog and reason over it without Audius watching."

### The tools

Nine, in three groups:

- **Discovery** — `list_datasets`, `describe`. What exists, how many records, which
  relation vocabulary, whether a relational axis is delivered at all.
- **The two axes** — `search` (semantic; exact cosine over all 25,372 records, embedded
  in-process) and `relations` / `neighbors` (relational; typed edge traversal).
- **Working tools** — `get`, `refresh`, `aggregate` (server-side group-by), `export`
  (dump records or the whole linkset for the agent's own BFS).

### The loop

```
list_datasets → describe(audius) → search(…) → relations(id) → neighbors(id, rel=…)
```

**`relations` before `neighbors`, always.** It returns one row per (relation, direction)
with a count, rather than the neighbours themselves:

```
relations(audius:genre:electronic)  →  degree 2958
   in  inGenre  2232   crosses=False
   in  worksIn   726   crosses=False
```

Two rows instead of 2,958 records. Skip it and an agent gets an arbitrary slice of a hub
with no signal that it saw 25 of three thousand — so `neighbors` also reports `total` and
`truncated`. Hub-aware traversal is the difference between an agent that explores a graph
and one that drowns in it.

### The hop that is the whole pitch

The federation is legible to an agent in three calls, with no cooperation from either
publisher:

```
relations(audius:artistref:E2O1R)   → out sameAs 1  crosses=True    ← sorted first
neighbors(…, rel="sameAs")          → audius:user:E2O1R  publisher=0x2222…
relations(audius:user:E2O1R)        → out created 41
```

The platform's stub, the artist's own node under a different wallet, then their real
catalog. `crosses=True` marks a group whose neighbour was published by someone else — the
edges no single publisher could have authored alone, surfaced first because they're the
interesting ones.

### Setup

```sh
pip install -e ".[agent]"
quickbeam cdn serve --cdn-dir ./audius-build/cdn --cors --port 8090   # must be running
```

`.mcp.json` at the repo root registers it over stdio; Claude Code picks it up with no
further configuration. The MCP server reads from that CDN — it is not a second copy of
the data.

---

## What to show, in order

1. **Search** — "dark acid groove house with a moody bassline." Results interleave both
   publishers; the badge on each card is the wallet that published it.
2. **Search the artist's name** — the platform's thin stub and the artist's own rich node
   come back side by side, from two different wallets.
3. **Traverse `sameAs`** — the two identities are asserted equal by linkset data, not by
   either party's database.
4. **Traverse `contains`** — a platform playlist whose tracks live in the artist's graph.
5. **The converged vocabulary** — both publishers independently derived `Genre: Electronic`
   and landed on the same vertex. Content addressing doing a schema registry's job.
6. **Switch to the agent** — the same three-call hop over MCP, to make the point that this
   is a data layer, not a web app.
7. **The close** — Audius' discovery today needs every artist's data inside Audius' index.
   Here the platform's graph has a hole shaped like the artist, the artist fills it from
   their own wallet, and search spans both anyway.

---

## Honest limits

- **This build has not settled on-chain.** Placeholder wallets, `local-prebake`
  provenance. The publish path is written and documented; it hasn't been run for this
  snapshot.
- **65.7 MB is a lot over conference wifi.** Fine served locally. Tags are 8,925 of 25,372
  points and every point costs about the same, so dropping Tag as a node type cuts it to
  roughly 42 MB at the cost of the `taggedWith` relation. Tags stay searchable either way.
- **Search is brute-force exact cosine**, not ANN. Correct and fast at 25k records; it is
  not the design for 25M.
- **The on-chain root is trusted, not proven.** `commit_state_root` has a merkle-proof TODO
  — the contract enforces the timeline and the compare-and-swap, but does not yet verify
  the graph behind the root.
- **Scale is unproven here.** This is one artist split out of a 46k-record crawl, not the
  full Audius catalog. The architecture doesn't change with size; the embedding and hosting
  bill does.

---

## Reproducing it

`audius-build/RUNBOOK.md` — Part 0 takes a fresh clone to a running demo, Part 1 rebuilds
the graph with one command, Part 2 is the on-chain publish. Component docs:
`audius-source/README.md` (the split, the relations, MCP), `audius-demo/README.md` (the
browser client).

The one thing git can't give you is the 104 MB crawl cache. Copy it for a byte-identical
demo, or re-crawl for the same shape with fresher records.

### The numbers

| | |
|---|---|
| Crawl | 46,260 records — the determinism boundary |
| Graph A — platform | 25,306 nodes / 107,454 edges, Disclosure's catalog removed |
| Graph B — Disclosure | 99 nodes / 300 edges |
| Converged vocabulary | 33 nodes, shared by both publishers with no coordination |
| Linkset | 113 cross-publisher edges |
| Served snapshot | 25,372 records @ 256-d, 107,867 edges, 16 relation types, 65.7 MB, 0 unresolved endpoints |
| Types | Track 11,112 · Tag 8,925 · Artist 4,399 · Playlist 789 · Genre 124 · Mood 23 |
| Embedding | `nomic-ai/nomic-embed-text-v1.5`, 256-d matryoshka slice, cosine |
