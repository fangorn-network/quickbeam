# Fangorn Playground

A barebones app to register schemas, publish data into the **real** embedding
pipeline (`quickbeam watch` → Qdrant), and search it. Four tabs:

1. **Schemas** — a text box with a default schema + a `Register` button; and a
   "summon" box that loads any schema back from the on-chain registry.
2. **Register data** — a form whose placeholders come from the selected schema.
   Stage several records, publish them as one commit.
3. **Published** — the current full record set per dataset (a big JSON viewer) plus
   the commit history.
4. **Search** — semantic search over the semantic CDN snapshot (`quickbeam cdn serve`).

## How publishing reaches the watcher

The `quickbeam watch` daemon only ingests **bundle** manifests (it edge-walks to
build documents). So a flat "schema + fields" is shaped server-side into a
single-node bundle: the server registers the node schema **and** a one-type bundle
around it, then publishes each record as a node with a trivial self-edge. Publishing
is git-native — `commitBundle` (a full snapshot on the dataset's HEAD) + `push` —
and the commit carries an embed contract (`nomic-embed-text-v1.5`, dim 256) so the
watcher indexes at the same dim the browser query embedder uses. The browser never
sees the key or the bundle plumbing.

Commits are **full snapshots** of the dataset (like a git working tree), not deltas
— the watcher tombstones anything present in the previous tip but absent from the
new one, so a delta commit would delete the rest. If the server loses its local
state but the chain has a tip, it reconstructs the dataset from IPFS (the git-native
clone property) before appending.

## Run

Three processes: Qdrant, the publish service, and the app — plus the watcher, which
you start with the command the Schemas tab prints after you register.

The data path is: **publish → watcher embeds into Qdrant → watcher bakes/appends the
semantic CDN → `cdn serve` → the app searches the served shards.** The watcher owns the
whole loop (bake-on-start + per-cycle delta append), so there's no manual bake step.
Search never touches Qdrant directly — it reads the semantic CDN.

```sh
# 0. Qdrant (the watcher embeds into it; the CDN is baked from it)
docker run -p 6333:6333 qdrant/qdrant

# 1. the publish service (owns the key + SDK). Tolerates a stray leading '.' on
#    PINATA_JWT and a trailing '/ipfs' on PINATA_GATEWAY (both common in .env).
cd playground/server
npm install
export DELEGATOR_ETH_PRIVATE_KEY=0x... PINATA_JWT=... PINATA_GATEWAY=https://…mypinata.cloud
export CHAIN_NAME=arbitrumSepolia
npm start                       # → http://localhost:8791

# 2. the app
cd playground
npm install
npm run dev                     # → http://localhost:5273  (proxies /api and /cdn)
```

Then, in the app:

1. **Schemas** → `Register schema`. Copy the `quickbeam watch …` command it prints.
2. Run that command — the real embedding daemon. Source your quickbeam `.env` first
   for `$BUILD_AUTH` (= `--graph-api-key … --ipfs-gateway … --ipfs-gateway-key …`),
   then leave it running. For the default `Fieldnote` schema it looks like:

   ```sh
   cd ~/fangorn/embeddings
   set -a; source quickbeam/.env; set +a           # exports $BUILD_AUTH, GRAPH_API_KEY, …

   quickbeam watch \
     --bundle playground.fieldnote.bundle.v1=<bundleSchemaId> \
     --root-type Fieldnote \
     --collection playground \
     --checkpoint-file ./db/playground_checkpoint.json \
     --poll-interval 30 \
     --cdn-dir ./cdn-playground --cdn-domain fieldnote \
     $BUILD_AUTH
   # (use `python -m quickbeam.cli watch …` if quickbeam isn't on PATH)
   ```

   It embeds into an isolated `playground` collection on `localhost:6333`.
   `IPFS_GATEWAY` ending in `/ipfs` is correct for the Python watcher (it appends
   `/<cid>`; only the TS SDK needed the trimmed form).

   Two things the printed command is careful about — both were real footguns:

   - **No `--dataset` filter.** Only the *first* publish emits a `ManifestPublished`
     event carrying the dataset name; every publish after that is a git-native tip
     UPDATE (`ManifestUpdated`), which carries **no** name. A `--dataset Fieldnote`
     filter therefore drops all your later commits and the watcher silently ingests
     nothing past the first publish ("No pending manifests after filters"). The bundle
     schema id already scopes the watch to this dataset, so the filter isn't needed.
   - **Isolated `--collection` + `--checkpoint-file`.** The CDN is baked from the
     whole collection, so sharing the default `fangorn` collection with other work
     leaks unrelated points into your snapshot. A dedicated collection keeps the
     playground clean; a per-collection checkpoint keeps its ingest cursor separate.
3. **Register data** → fill fields, `Publish`. Publishing writes on-chain; the
   watcher embeds within a poll cycle (plus a little subgraph indexing lag), then
   appends the new points to the CDN (see step 4) — no manual step.
4. **The watcher handles the CDN for you.** With the `--cdn-dir/--cdn-domain` flags
   above, the watcher bakes an initial snapshot on startup (if the domain isn't baked
   yet) and appends each embed cycle as an immutable delta shard. You only need to
   serve it:

   ```sh
   cd ~/fangorn/embeddings
   quickbeam cdn serve --cdn-dir ./cdn-playground --port 8090 --cors  # app proxies /cdn → :8090
   ```

   (Manual `quickbeam cdn bake --collection playground --cdn-dir ./cdn-playground` still
   works for a full re-bake, but you shouldn't need it — the live delta path keeps the
   snapshot current.)
5. **Search** → type a meaning. Results come from the served CDN shards. After you
   publish more data, hit **Reload snapshot** to pull the newly-appended delta shards.

The app degrades honestly: with no server keys, the Schemas/Data steps are disabled
with a note; Search works once the CDN is baked + served.

## Layout

```
playground/
  src/
    App.tsx              the four tabs
    components/bits.tsx   Cid chip, JSON viewer
    lib/
      api.ts             client for the publish service
      cdn.ts             semantic-CDN read client (downloads + ranks served shards)
      embed.ts           in-browser QUERY embedder (== quickbeam/embeddings.py)
      types.ts
  server/
    index.mjs            node:http wrapper over @fangorn-network/sdk
```

## What maps to what

| In the app          | Under the hood                                                     |
|---------------------|--------------------------------------------------------------------|
| Register schema     | `schema.register` (node resolver) + `schema.register` (bundle)     |
| Summon schema       | `schema.get(nameOrId)` — read back from the on-chain registry      |
| Publish records     | `publisher.commitBundle({ parents, message, embed })` + `push`     |
| (server resilience) | `resolveTip` + `ObjectStore` + `readBundle` — clone from the tip   |
| Search              | download `cdn serve` shards → in-browser `embedQuery` + cosine     |
