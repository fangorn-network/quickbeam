# DO NOT BUILD LOCAL

**TL;DR:** The publish (one bundle tx) is laptop-safe. **The build is NOT.** Running
`quickbeam build --bundle` against a single full-graph manifest will OOM a laptop —
it loads every node in the manifest into RAM to fold neighbors. Build on a big box,
or publish as a few large shards (see below).

---

## What now works (the bundle chunking fix)

`BundleBuilder` was changed so a bundle is chunked into many ~1000-entry merkle
leaves (nodes per type + edges) under **one root → one tx** — instead of one giant
edge chunk that hit V8's ~512 MB `JSON.stringify` cap. Files changed (SDK repo
`/home/driemworks/fangorn/fangorn`, + this repo's `embeddings.py`):

- `src/roles/publisher/builders/bundle.ts` — streaming + 1000-leaf chunking, O(n+e)
  validation (optional), position-based leaf↔chunk mapping.
- `src/roles/publisher/types.ts` — `BundleManifest.edgeChunk` → `edgeChunks[]`
  (old single `edgeChunk` still readable).
- `src/roles/publisher/index.ts` — `publishBundle` accepts streams + `chunkSize`/`validate`; `readBundle` reads `edgeChunks`.
- `quickbeam/embeddings.py` — build reads `edgeChunks` (falls back to `edgeChunk`).
- `src/test/publish_bundle.ts` — one-shot streamed publisher (one tx).

`RecordSetBuilder` (individual-schema record-sets) was NOT touched.

---

## Publish = one tx, laptop-safe

Streams every node + edge into ONE `publishBundle`. With `validate:false` there are
no unbounded structures (buffers ≤ chunkSize); peak RAM is low hundreds of MB.

```sh
cd /home/driemworks/fangorn/fangorn
# optional cheap dry run (tiny upload, one tx):
pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir /home/driemworks/fangorn/embeddings/stage_volumes --limit 5000
# full one-tx run: drop --limit
pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir /home/driemworks/fangorn/embeddings/stage_volumes
```

Prints the bundle id + the build command.

---

## Build = DO NOT do it on a laptop (for a single full-graph manifest)

`build_bundle_joined_data` (embeddings.py) loads a **whole manifest's nodes** into a
`nodes_by_id` dict to fold neighbors per root. For one manifest holding ~44M nodes
that's tens of GB → OOM on a laptop. The publish being one tx is exactly what makes
the manifest too big to build locally.

Two ways to make the build feasible:

1. **Build on a big-RAM box** (cloud VM) against the single one-tx manifest:
   ```sh
   quickbeam build --bundle "fangorn.mb.creativecore.v1=<id>" --root-type Recording --reset
   ```
2. **Publish as a few large shards** (`--shard-roots N`) — each shard is one tx, one
   self-contained manifest (its roots + the neighbor nodes their edges point at + those
   edges), so the builder consumes them **one at a time within RAM** (laptop-OK). The
   chunking fix removes the old per-shard limits, so shards can be large → a handful of txs.

   ```sh
   cd /home/driemworks/fangorn/fangorn
   pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
     --input-dir /home/driemworks/fangorn/embeddings/stage_volumes \
     --root-type Recording --shard-roots 200000 --sort-mem 256M --concurrency 4
   ```

   How it works: sort-merge (root-origin edges + roots sorted with GNU `sort`, neighbor
   node fields held in RAM) → each shard = N roots + their neighborhood → one chunked
   `publishBundle` (one tx). Resumable via a ledger (`tmp/bundle-<dataset>.json`).
   Knobs: `--shard-roots` (↑ = fewer txs but more publish *and* build RAM; ~200k is a
   laptop-reasonable start), `--index-dir` (sort spill, real disk ~2× edge size),
   `--sort-mem`, `--sort-parallel`, `--ledger`, `--max-retries`, `--limit` (dry run).

   Publish-side memory note: sharded publish still holds the needed neighbor node fields
   in RAM (~GBs for the full graph) — heavier than the one-shot stream, but it's the cost
   of building self-contained shards. Close other apps, or run publish on a roomier box;
   the *build* is the part this makes laptop-safe.

**Rule:** one-tx publish ⇒ build on big RAM. Laptop build ⇒ publish in a few large shards (`--shard-roots`).

---

## Before re-publishing: unpin the old data

See `DO_THIS_TOMORROW.md` for the exact `pinata.mjs delete-pattern` commands. The
old denormalized record-set (`chunk:*`, `manifest:record-set:*`) and the partial
`publishBundle` attempt (`bundle-*`, `manifest:bundle:*`) are both obsolete now.
Delete them BEFORE the new run. Leave `schema:*` blobs alone.


---

All changes implemented, SDK typechecks clean (0 errors), embeddings.py compiles. Nothing run. Here's the review.

What changed (4 files, surgical — RecordSetBuilder untouched, so individual schemas still work)
builders/bundle.ts — chunk() now streams nodes (buffered per type) and edges, flushing ~1000-entry leaves instead of one-chunk-per-type + one giant edge chunk. Accepts array | AsyncIterable. validate is optional (skips the in-memory node-id map for huge streamed runs; per-record schema validation always runs). Cardinality is O(n+e), only for constrained shapes.
publisher/types.ts — BundleManifest.edgeChunk → edgeChunks[] (+ optional edgeChunk kept so old manifests still read).
publisher/index.ts — publishBundle accepts streams + chunkSize/validate; readBundle reads edgeChunks ?? [edgeChunk].
embeddings.py + cleanup-embeddings-testdata.ts — read edgeChunks (fall back to edgeChunk).
Plus a one-shot publisher: src/test/publish_bundle.ts streams the whole graph into a single publishBundle → one tx.

Dan-Boneh review of the commitment (the part that must be exactly right)
Deterministic leaf order: every chunk carries a monotonic seq (yield order); compareChunks sorts by seq — a strict total order, no ties → the merkle tree is reproducible. publish() pushes chunks in upload-completion order, but the sort restores yield order.
Leaf↔chunk mapping is position-based now: assemble maps ctx.chunks[i] → ctx.leaves[i] (they're aligned because leaves = chunks.map(...) after the sort). This replaces the previous fragile ctx.leaves[c.index] lookup, which was only correct because the old builder emitted exactly one chunk per type in a fixed order. With sub-chunking that assumption would have broken silently — position-mapping is robust.
Self-consistent / verifiable: nodeChunks ++ edgeChunks in the manifest is exactly the seq order the tree was built over, so a verifier can reconstruct leaf positions; recorded leaf hashes match tree leaves. manifest.tree is the full layer set, unchanged.
Backward compatible: old single-edgeChunk manifests still read (SDK + builder + cleanup all fall back).
Memory (your requirement)
The one-shot publish is bounded: with validate:false there are no unbounded structures in the builder (buffers ≤ chunkSize per type + edge buffer); publish() holds ~98k chunk refs + tree + manifest ≈ low hundreds of MB. One tx, laptop-safe publish. ✓

One honest caveat — the build, not the publish
build_bundle_joined_data loads a whole manifest's nodes into RAM to fold neighbors. A single 44M-node manifest = tens of GB on the build side — fine to publish on a laptop, not to build on one. The chunking fix also unlocks a middle option: a handful of large bundle shards (each one tx, no O(n²), no 512MB wall) that the builder consumes one-at-a-time within RAM. So you can choose: literally one tx (needs a big build box) or ~5–10 large-shard txs (laptop-buildable). publish_bundle.ts does one tx today; a --shard-roots N option is a small add if you want shards.

To run (your call — I'm not running it)

cd /home/driemworks/fangorn/fangorn
# optional cheap dry run first:
pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir /home/driemworks/fangorn/embeddings/stage_volumes --limit 5000
# full one-tx run: drop --limit
Want me to add --shard-roots (few-tx, laptop-buildable) before you commit to the upload, or leave it as the single-tx publish?

write a summary to a ew markdown, say DO_NOT_BUILD_LOCAL.md?
Write DO_NOT_BUILD_LOCAL.md
77 lines
# DO NOT BUILD LOCAL

**TL;DR:** The publish (one bundle tx) is laptop-safe. **The build is NOT.** Running
`quickbeam build --bundle` against a single full-graph manifest will OOM a laptop —
it loads every node in the manifest into RAM to fold neighbors. Build on a big box,
or publish as a few large shards (see below).

---

## What now works (the bundle chunking fix)

`BundleBuilder` was changed so a bundle is chunked into many ~1000-entry merkle
leaves (nodes per type + edges) under **one root → one tx** — instead of one giant
edge chunk that hit V8's ~512 MB `JSON.stringify` cap. Files changed (SDK repo
`/home/driemworks/fangorn/fangorn`, + this repo's `embeddings.py`):

- `src/roles/publisher/builders/bundle.ts` — streaming + 1000-leaf chunking, O(n+e)
  validation (optional), position-based leaf↔chunk mapping.
- `src/roles/publisher/types.ts` — `BundleManifest.edgeChunk` → `edgeChunks[]`
  (old single `edgeChunk` still readable).
- `src/roles/publisher/index.ts` — `publishBundle` accepts streams + `chunkSize`/`validate`; `readBundle` reads `edgeChunks`.
- `quickbeam/embeddings.py` — build reads `edgeChunks` (falls back to `edgeChunk`).
- `src/test/publish_bundle.ts` — one-shot streamed publisher (one tx).

`RecordSetBuilder` (individual-schema record-sets) was NOT touched.

---

## Publish = one tx, laptop-safe

Streams every node + edge into ONE `publishBundle`. With `validate:false` there are
no unbounded structures (buffers ≤ chunkSize); peak RAM is low hundreds of MB.

```sh
cd /home/driemworks/fangorn/fangorn
# optional cheap dry run (tiny upload, one tx):
pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir /home/driemworks/fangorn/embeddings/stage_volumes --limit 5000
# full one-tx run: drop --limit
pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir /home/driemworks/fangorn/embeddings/stage_volumes
```

Prints the bundle id + the build command.

---

## Build = DO NOT do it on a laptop (for a single full-graph manifest)

`build_bundle_joined_data` (embeddings.py) loads a **whole manifest's nodes** into a
`nodes_by_id` dict to fold neighbors per root. For one manifest holding ~44M nodes
that's tens of GB → OOM on a laptop. The publish being one tx is exactly what makes
the manifest too big to build locally.

Two ways to make the build feasible:

1. **Build on a big-RAM box** (cloud VM) against the single one-tx manifest:
   ```sh
   quickbeam build --bundle "fangorn.mb.creativecore.v1=<id>" --root-type Recording --reset
   ```
2. **Publish as a few large shards** (`--shard-roots N`) — each shard is one tx, one
   self-contained manifest (its roots + the neighbor nodes their edges point at + those
   edges), so the builder consumes them **one at a time within RAM** (laptop-OK). The
   chunking fix removes the old per-shard limits, so shards can be large → a handful of txs.

   ```sh
   cd /home/driemworks/fangorn/fangorn
   pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
     --input-dir /home/driemworks/fangorn/embeddings/stage_volumes \
     --root-type Recording --shard-roots 200000 --sort-mem 256M --concurrency 4
   ```

   How it works: sort-merge (root-origin edges + roots sorted with GNU `sort`, neighbor
   node fields held in RAM) → each shard = N roots + their neighborhood → one chunked
   `publishBundle` (one tx). Resumable via a ledger (`tmp/bundle-<dataset>.json`).
   Knobs: `--shard-roots` (↑ = fewer txs but more publish *and* build RAM; ~200k is a
   laptop-reasonable start), `--index-dir` (sort spill, real disk ~2× edge size),
   `--sort-mem`, `--sort-parallel`, `--ledger`, `--max-retries`, `--limit` (dry run).

   Publish-side memory note: sharded publish still holds the needed neighbor node fields
   in RAM (~GBs for the full graph) — heavier than the one-shot stream, but it's the cost
   of building self-contained shards. Close other apps, or run publish on a roomier box;
   the *build* is the part this makes laptop-safe.

**Rule:** one-tx publish ⇒ build on big RAM. Laptop build ⇒ publish in a few large shards (`--shard-roots`).

---

## Before re-publishing: unpin the old data

See `DO_THIS_TOMORROW.md` for the exact `pinata.mjs delete-pattern` commands. The
old denormalized record-set (`chunk:*`, `manifest:record-set:*`) and the partial
`publishBundle` attempt (`bundle-*`, `manifest:bundle:*`) are both obsolete now.
Delete them BEFORE the new run. Leave `schema:*` blobs alone.
You've hit your session limit · resets 5am (America/Chicago)s