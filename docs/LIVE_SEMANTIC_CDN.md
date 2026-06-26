# Live Semantic CDN: gossip the deltas, route by geometry

A design for two things the static [Semantic CDN](SEMANTIC_CDN.md) doesn't yet do:

1. **Live** — new data published to Fangorn flows to where it's needed without a manual
   re-bake or a 60s poll on the consumer side.
2. **Selective at scale** — a user with a 15 GB+ catalog available pulls only the small
   slice they actually care about, **semantically**, and **without telling the network
   what that slice is**.

Both collapse onto one primitive, so this doc builds to it rather than treating them
separately. It is a design, not yet shipped; it extends the bake/serve/pull machinery in
[`quickbeam/cdn.py`](../quickbeam/cdn.py) and the embed daemon in
[`quickbeam/watcher.py`](../quickbeam/watcher.py).

---

## 0. Where we are

The static CDN gives us the two **extremes** of one axis — bandwidth vs. intent exposure:

```
   server.py /search              pull whole domain → query local
   ─────────────────              ────────────────────────────────
   tiny bandwidth                 zero intent exposure
   total intent exposure          download the entire slice
   (node sees every query vec)    (dead at 15 GB)
```

And it is **batch**: `quickbeam cdn bake` is a manual, point-in-time cut, while
[`watcher.py`](../quickbeam/watcher.py) only polls the subgraph (`--poll-interval 60`)
and upserts into Qdrant. Nothing pushes a freshly embedded record toward a consumer, and
nothing lets a consumer take less than a whole domain. This doc fills the middle of the
axis and adds the missing push.

---

## 1. Live: gossip announcements, not embeddings

**Rule: never gossip vectors. Gossip pointers; keep content-addressed shards as the data
plane.** Everything needed for this already exists in the static design — shards are
immutable and `sha256`-stamped, so they're already content addresses (CDN today, IPFS /
bitswap with zero format change, exactly as `SEMANTIC_CDN.md` notes).

```
   manifest published on Fangorn
            │  (subgraph event — watcher already sees this)
            ▼
   watcher embeds  ──►  _embed_and_upload (Qdrant, as today)
            │
            ├─► append immutable shard-NNNN-<hash>.ndjson.gz   (data plane: CDN/IPFS)
            │
            └─► gossipsub PUBLISH on the slice's topic           (control plane)
                {topic, new_shard_cid, +N points, manifest_version}
            ▼
   subscribers hear the delta → pull just that shard by cid → upsert local Qdrant
```

The gossip message is a few hundred bytes: *what changed, where to get it, what version
it brings the manifest to*. The bytes still move through the existing immutable-shard
path (`pull.py` verifies `sha256`, resumes via Range, upserts with the same `uuid5` ids —
all unchanged).

**Topic = slice.** Your [`domains.json`](../domains.json) filters map straight onto
pubsub topics. The watcher already knows the `entityType` / `owner` of each record (it
accumulates `type_counts` during bake; it has `owner`/`name` per manifest during watch),
so it knows which topic(s) a new shard belongs to.

This keeps the watcher's poll as the **chain→embedder bridge** and makes gossip the
**embedder→consumer bridge**. "Quickbeamed to where it's needed" = published on the topic
for that slice; the people who care are exactly the ones subscribed.

> **The catch, flagged early because it's the whole story:** *subscribing to a topic is
> an intent signal.* Which slices you follow leaks the same way which domain you pull
> leaks. So topic granularity is a privacy knob — identical to the domain-granularity knob
> already in `SEMANTIC_CDN.md`, and to the cell-granularity knob in §2. Same knob, three
> places. Hold that thought.

---

## 2. Selective at scale: route by geometry, not by name

A domain today is **operator-declared**: a human names a filter (`entityType`, `owner`)
in `domains.json`. That doesn't subdivide a 15 GB corpus finely enough, and it can't,
because humans don't know where in the model's space the interesting boundaries fall.

The scalable partition is **geometry-declared** — learned from the embeddings themselves.
The key asymmetry that makes this work:

> **The power to *route* a query is tiny. The vectors it routes *to* are huge.**

So split them and make the cheap half public:

### Public router, private leaves

1. **Cluster the whole catalog into centroids** (IVF / k-means tree / an HNSW coarse
   layer). A few thousand centroids × 256-d ≈ **a couple MB**. Publish this `index.json`
   and let **everyone download all of it**. Downloading the *entire* router leaks nothing
   about which part you'll use — the same logic that makes pulling a whole domain private,
   but now it costs megabytes, not gigabytes.

2. **Each leaf cell = one content-addressed shard** — the vectors whose nearest centroid
   is `c_k`. This is *exactly today's shard system* with the partition function swapped:
   - today:    `shard = { entityType ∈ {Place, Event} }`   (operator-declared)
   - proposed: `shard = { argmin_k ‖v − c_k‖ }`             (geometry-declared)

3. **The user descends locally.** Embed the query on the user's machine (`server.py`
   already does this), walk the public centroids locally, pick the top-k cells, fetch
   **only those shards**. The query never leaves the machine; the CDN sees only *which
   cells* — never what was asked.

Domains and cells **coexist**: domains are the human-meaningful top cut (music vs.
venues), cells are the machine-meaningful sub-cut inside each.

### The recursion (the "fractal")

A cell that's still too big gets its **own** centroid sub-index → sub-shards. The catalog
becomes a tree where **every node is `(tiny public router, big private leaves)`**:

```
   index.json                              ~2 MB, public, everyone has it
   ├── cell 0  → shard-0  (small? leaf)
   ├── cell 1  → index-1.json  (still big → recurse)
   │             ├── cell 1.0 → shard-1.0
   │             └── cell 1.7 → shard-1.7   ← query lands here
   └── cell 2  → shard-2
```

A user descends only as deep as needed and pays bandwidth along **one root-to-leaf path**,
not the whole tree — log-depth, not linear. You never touch the 99% you don't care about,
and you never told anyone which 1% you did. That is the structure that makes 15 GB
tractable.

---

## 3. The privacy ladder (and where it frays)

"Fetch cell X without the server learning X" is **Private Information Retrieval**, and
full PIR is expensive. The hierarchy is what makes it *affordable* — so stage it, and ship
the cheap rungs first:

| Level | Mechanism | Anonymity set | Cost | Status |
|---|---|---|---|---|
| **L0** | pull whole domain (today) | the domain | gigabytes | shipped |
| **L1** | public centroid descent → fetch matching cell(s) | one cell | one shard | small change to bake |
| **L2** | k-anonymous fetch: real cell + decoys to flatten the access distribution | k cells | k shards | cover traffic |
| **L3** | PIR / oblivious retrieval — **at the leaf only** | the cell, hidden | one small-DB PIR | paranoid tier |

The lever at **L1** is **cell coarseness**: coarse cells co-locate many unrelated concepts,
so each fetch is a large anonymity set — the dual of the domain-granularity knob already in
`SEMANTIC_CDN.md`. Put a dumb cache/CDN in front (requests already unlinkable to content
because filenames are content hashes) and L1 is cheap and mostly built.

**Why the hierarchy is the payoff, not just bandwidth:** PIR cost scales with database
size. A cell is small. The tree converts one intractable *PIR-over-15 GB* into a free
public descent plus a cheap *PIR-over-one-cell*. The fractal structure buys privacy, not
only speed.

**Where it genuinely frays — so nobody is surprised:**

- **Boundary recall.** True nearest neighbors straddle cell borders (IVF's `nprobe > 1`).
  Better recall = fetch neighboring cells = more bytes **and** more leak. There is no free
  corner here; recall, bandwidth, and privacy trade against each other.
- **Rebalancing vs. immutability.** As the corpus grows, centroids drift and hot cells must
  split. Re-minting every shard is unacceptable. Resolution: **append-only cells + explicit,
  versioned split events** in the public index — and split events gossip naturally on the
  live layer (`cell 4.17 → {4.17.0, 4.17.1}`).
- **Subscription = intent, again** (§1). The live layer's topic subscription leaks
  region-of-interest. Same fix as fetch: subscribe to **coarse regions, not points**.

---

## 4. The synthesis

Both gaps are the same primitive:

> **A versioned, public semantic routing tree over content-addressed leaf shards.**

- **Live** = gossiped deltas keyed by *cell*. The watcher assigns each newly embedded
  record to a cell via the same centroids, appends a shard, gossips the delta on that
  region's topic.
- **Private discovery** = a *local* descent of the public tree that fetches *cells*.

The one reframe to internalize: today a domain is **operator-declared**; the scalable
version is **geometry-declared** — learned from the embeddings, which is *why* it can be
recursive. You index the catalog not by what humans named it but by where it lands in the
model's space, and that index is small enough to hand to everyone.

---

## 5. Build order

1. **Router bake (L1).** Extend `cdn.py`: after scrolling a domain, fit IVF centroids over
   its vectors, assign each point to a cell, and emit cells as the existing shard files
   plus a public `index.json` (`{centroids, dim, cell → shard mapping, version}`). This is
   the load-bearing assumption — everything else rests on the router being small and
   queryable locally, so prototype it first and measure router size vs. recall.
2. **Local descent in `pull` / `server`.** Given `index.json` + a local query embedding,
   pick top-k cells and fetch only those shards. Reuse `pull.py`'s verify/resume/upsert.
3. **Gossip control plane.** Watcher publishes `{topic, shard_cid, +N, version}` after
   `_embed_and_upload`; a subscriber loop pulls announced shards. Topic = domain at first,
   = cell once the router exists.
4. **Recursion + split events.** Sub-index a cell when it exceeds a size threshold; emit
   versioned split events on both the manifest and the gossip topic.
5. **L2 cover traffic, then L3 leaf-PIR** — deferred until L1 validates that geometric
   cells give acceptable recall at a useful anonymity-set size.
