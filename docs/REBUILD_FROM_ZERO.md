# MusicBrainz → Fangorn: Rebuild From Zero

A complete runbook for regenerating the MusicBrainz datasets this repo produces,
on a fresh machine. There are **two independent data pipelines**, both of which
emit the same Fangorn node/edge file format (`volume_<n>_<schema>.json` arrays of
`{name, fields}` plus a `volume_<n>_edges.json` array of `{rel, from, to}`):

| Pipeline | Command | Source | Output |
|---|---|---|---|
| **A. Flat track corpus** | `quickbeam data mb` | MusicBrainz *release* JSON dump (~23 GB) | A quality-ranked list of up to N tracks |
| **B. Creative-core knowledge graph** | `quickbeam data mbpg` | MusicBrainz **Postgres** dump (~8 GB) | A typed graph over a selectable set of entities (Artist / ReleaseGroup / Release / Recording / Work / Area / Place / Event / Instrument) + auto-discovered relationships |

Pipeline A is the fast, self-contained track extractor. Pipeline B is the
"convert a relational DB into a semantic space" showcase — it reads a declarative
**entity registry**, auto-discovers the `l_*` link tables that connect whatever
entities you select, and (optionally) applies a quality cut so you extract a
ranked subgraph rather than the whole noisy DB. You can run either or both.

Downstream, `quickbeam data schemagen` derives Fangorn schemas + a bundle shape
from the graph, which are registered and published to Fangorn (§5) and then
embedded into Qdrant with `quickbeam build` (§6), then browsed (§7).

---

## TL;DR — recreate the whole thing (Pipeline B → browser)

Assuming the Postgres dump is already imported (§3.1–3.2) and the graph is already
extracted to `stage_volumes/` (§3.3), the steps to get back to a browsable graph:

```sh
# 1. (re)generate schemas + bundle from the extracted graph
cd /home/driemworks/fangorn/embeddings && source venv/bin/activate
quickbeam data schemagen --volume 1                                    # §5

# 2. publish the graph — sharded so it stays CONNECTED on a laptop.
#    ⚠️ never use --limit for a real publish (it truncates edges → dead links). §5
cd /home/driemworks/fangorn/fangorn
dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir /home/driemworks/fangorn/embeddings/stage_volumes \
  --shard-roots 200000 --root-type Recording          # prints the bundle <name>=<id>

# 3. embed into Qdrant, one record per profile view. §6
docker start qdrant-core 2>/dev/null || \
  docker run -d --name qdrant-core -p 6333:6333 -p 6334:6334 qdrant/qdrant
cd /home/driemworks/fangorn/embeddings
quickbeam build --bundle "<name>=<id>" \
  --root-profile track --root-profile artist --root-profile place --root-profile event \
  --graph-api-key <key> --ipfs-gateway https://<gateway>.mypinata.cloud/ipfs \
  --dim 256 --umap --reset

# 4. browse it. §7
cp stage_volumes/schemas/*.json examples/public/schemas/   # keep schemas in sync
cd examples && npm install && npm run dev                   # → http://localhost:5173
```

The rest of this doc is the full, from-zero version of each step.

---

## 0. Prerequisites

- Linux, **Python 3.12+**
- **Docker** + Docker Compose v2 (`docker compose version`) — required for Pipeline B and Qdrant
- CLI tools: `git`, `curl`, `bzip2`, `xz`
- **Disk**: ~30 GB for Pipeline A; ~130 GB for Pipeline B (8 GB dump + ~80–120 GB Postgres). Check with `df -h`.
- RAM: 8 GB+ (Pipeline A Pass 1 holds an artist-id set; Pipeline B streams via server-side cursors so it stays flat)

---

## 1. Clone & install

```sh
git clone <this-repo> fangorn-embeddings
cd fangorn-embeddings/embeddings        # the quickbeam package lives here

python -m venv venv
source venv/bin/activate

pip install -e ".[cpu]"        # or ".[gpu]" for CUDA-accelerated embeddings
```

This installs the `quickbeam` CLI (includes `psycopg[binary]` for Pipeline B).
Verify:

```sh
quickbeam --help
quickbeam data --help          # fetch | mb | mbpg | osm
```

---

## 2. Pipeline A — Flat track corpus (quality-gated)

Extracts up to `--target-count` tracks from the MusicBrainz release JSON dump,
**ranked by popularity** (ListenBrainz artist listen counts) and filtered for
commercial quality (ISRC / Official status), then filled for breadth.

```sh
quickbeam data mb --target-count 10000000 --volume 1
```

What happens:
1. **Download** — auto-discovers the latest `release.tar.xz` (~23 GB) into
   `./stage_volumes/cache/`. Resumable, multi-connection, and **integrity-checked**
   (xz magic header + `.incomplete` sentinel) so a half-finished/corrupt cache is
   detected and re-downloaded instead of crashing the decompressor.
2. **Pass 1** — one decompression pass to index distinct artist MBIDs.
3. **ListenBrainz enrichment** — batches artists through the popularity API
   (1000/req, rate-limit aware). Cached to `cache/artist_popularity.json`, so
   re-runs are instant. Pass `--lb-token <token>` (or env `LISTENBRAINZ_TOKEN`)
   for higher limits.
4. **Pass 2 + assembly** — scores & shards every track into high/mid/low tiers,
   then emits HIGH (charting artists) → MID (ISRC/Official corpus) → LOW until the
   target is met, with a per-artist cap for variety.

Output (in `./stage_volumes/`): `volume_1_tracks.json`, `volume_1_taxonomies.json`,
`volume_1_edges.json`.

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--target-count N` | 1,000,000 | How many tracks to emit |
| `--no-quality-gate` | (off) | Emit raw dump order (≈random), legacy behavior |
| `--no-popularity` | (off) | Skip ListenBrainz; score from in-dump signals only |
| `--cross-release-freq` | off | Add a per-track "hit" signal (more RAM) |
| `--per-artist-cap N` | 400 | Max tracks per low-popularity artist (variety) |
| `--force-download` | off | Discard cached dump and re-download |

> If you ever see `ReadError: empty file` it means a corrupt cached dump — re-run
> with `--force-download` (the integrity guard now catches this up front).

---

## 3. Pipeline B — Creative-core knowledge graph (Postgres)

This stands up MusicBrainz in a real Postgres (via the official
`musicbrainz-docker`) and runs `quickbeam data mbpg` against it.

### 3.1 Download the Postgres dump

```sh
mkdir -p ~/stage_volumes/pgdump && cd ~/stage_volumes/pgdump
LATEST=$(curl -s https://data.metabrainz.org/pub/musicbrainz/data/fullexport/LATEST)
BASE="https://data.metabrainz.org/pub/musicbrainz/data/fullexport/$LATEST"

curl -L -C - -O "$BASE/mbdump.tar.bz2"          # core entities + l_* link tables (~7.3 GB)
curl -L -C - -O "$BASE/mbdump-derived.tar.bz2"  # tags / genres / ratings (~0.5 GB)

# Integrity check (recommended before the multi-hour import):
bzip2 -t mbdump.tar.bz2 && bzip2 -t mbdump-derived.tar.bz2 && echo "dumps OK"
```

We only need **core + derived** for the creative core — the other five dumps
(cdstubs, cover-art, event-art, stats, wikidocs) are skipped.

### 3.2 Set up musicbrainz-docker (database only)

```sh
cd ~
git clone https://github.com/metabrainz/musicbrainz-docker.git
cd musicbrainz-docker
```

**(a)** Trim the required dump list so `createdb.sh` doesn't demand the five dumps
we didn't download. Edit `build/musicbrainz/scripts/createdb.sh`, find the
`fullexport` `DUMP_FILES=( … )` block and reduce it to:

```sh
        DUMP_FILES=(
            mbdump.tar.bz2
            mbdump-derived.tar.bz2
        );;
```

**(b)** Create `docker-compose.override.yml` to (1) expose Postgres on a stable
host port for the ingester, and (2) **bind-mount the edited `createdb.sh` over the
image's copy**. The bind-mount matters: `docker compose build` caches the
`COPY scripts/*` layer, so editing the script alone often does *not* make it into
the image — the mount guarantees the trimmed version is used:

```yaml
services:
  db:
    ports:
      - "5432:5432"
  musicbrainz:
    volumes:
      - ./build/musicbrainz/scripts/createdb.sh:/usr/local/bin/createdb.sh:ro
```

Sanity-check the container sees the trimmed list before importing:

```sh
docker compose run --rm --entrypoint sh musicbrainz \
  -c "grep -A4 'fullexport' /usr/local/bin/createdb.sh | grep mbdump"
# should list ONLY mbdump.tar.bz2 and mbdump-derived.tar.bz2
```

**(c)** Build, start the DB, load the dumps, and import:

```sh
# Build just the two images we need (slow, one-time)
docker compose build db musicbrainz

# Start Postgres
docker compose up -d db

# Copy the downloaded dumps into the import volume (creates it)
docker run --rm \
  -v musicbrainz-docker_dbdump:/media/dbdump \
  -v ~/stage_volumes/pgdump:/src:ro \
  alpine cp /src/mbdump.tar.bz2 /src/mbdump-derived.tar.bz2 /media/dbdump/

# Import — this is the long pole (~1–3 h). Skips Solr/search entirely.
docker compose run --rm musicbrainz createdb.sh
```

Verify the import:

```sh
docker compose exec -T db psql -U musicbrainz -d musicbrainz_db \
  -c "SELECT count(*) FROM recording;"   # expect tens of millions
```

> We deliberately do **not** run the `search` / `indexer` (Solr) services — the
> ingester only needs the database.

### 3.3 Run the ingester

Back in the embeddings repo (venv active):

```sh
# Smoke test first — 100 rows per query, validates the SQL against the live schema
quickbeam data mbpg --limit 100

# Full run (all 9 registry entities, all relationships)
quickbeam data mbpg --volume 1
```

Default connection is `postgresql://musicbrainz:musicbrainz@localhost:5432/musicbrainz_db`
(override with `--dsn` or env `MB_PG_DSN`).

**The entity registry.** Which tables become nodes is driven by a declarative
`ENTITIES` registry in `quickbeam/pipelines/mb_pg.py` — nine entities ship today:

| Key | Node type | File stem |
|---|---|---|
| `artist` | Artist | `artists` |
| `release_group` | ReleaseGroup | `releasegroups` |
| `release` | Release | `releases` |
| `recording` | Recording | `recordings` |
| `work` | Work | `works` |
| `area` | Area | `areas` |
| `place` | Place | `places` |
| `event` | Event | `events` |
| `instrument` | Instrument | `instruments` |

The music five use curated SELECTs; `area`/`place`/`event`/`instrument` use a
**convention-based** SQL builder that introspects the live schema (`gid`, `name`,
`comment`, plus `<entity>_type`/dates/rating/`<entity>_tag` *only where those
companion tables/columns exist*). To add another table, add one `Entity(...)` line
to the registry — nothing else changes. `--entities` selects a subset by key.

**Auto-discovered relationships.** The ingester probes for an `l_<a>_<b>` link
table for every ordered pair of *selected* entities (so the relationships you get
depend on what you select). Edges carry the typed relationships pulled from those
`l_*` tables (`performanceOf`, `composer`, `producer`, `vocal`, `memberOfBand`,
`samples`, `remixOf`, …) with `rel` taken straight from `link_type.name`, plus
structural edges (`hasRelease`, `hasTrack`, `byArtist`).

**Quality cut (optional).** By default `mbpg` extracts every row. Pass any of the
quality flags below and it switches to subgraph mode: it ranks each entity by a
score (community **rating** + `w_tags·ln(1+tag votes)`), keeps the top-N **seeds**
per entity, then expands one hop into neighbors via the link tables (capped per
seed) — so you get a dense, popular subgraph instead of MusicBrainz noise. All of
this runs server-side in Postgres TEMP tables in a single transaction.

Output (in `./stage_volumes/`): one `volume_1_<stem>.json` per selected entity
(e.g. `volume_1_artists.json`, …, `volume_1_places.json`) plus
`volume_1_edges.json`.

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--entities a,b,c` | all 9 | Subset of registry keys (`artist,release_group,release,recording,work,area,place,event,instrument`) |
| `--limit N` | 0 (all) | Per-query row cap for testing |
| `--no-edges` | (edges on) | Nodes only |
| `--no-structural-edges` | (on) | Relationship-system edges only (skip hasRelease/hasTrack/byArtist) |
| `--target-count N` | 0 (off) | Top-N seeds **per entity** by quality score (enables quality mode) |
| `--targets "E=N,…"` | "" | Per-entity overrides, e.g. `recording=1000000,artist=300000` |
| `--neighbor-cap N` | 50 | Max 1-hop neighbors per seed per relationship table (0 = unlimited) |
| `--min-score F` | 0.0 | Drop seeds scoring below this (enables quality mode) |
| `--w-rating F` | 1.0 | Weight on community rating (0–100) |
| `--w-tags F` | 20.0 | Weight on `ln(1+tag votes)` |
| `--dry-run` | off | Print per-entity seed counts under the current cut and exit |

> **Quality mode is enabled** when `--dry-run`, `--min-score > 0`, or any target
> (`--target-count`/`--targets`) is set. Example quality run:
> ```sh
> quickbeam data mbpg --volume 1 \
>   --targets "recording=1000000,artist=300000,release=300000" \
>   --neighbor-cap 50
> ```

---

## 4. Where the output goes

Both pipelines write Fangorn-format files under `./stage_volumes/`:

- **Node files** — JSON array of `{ "name": "<id>", "fields": { … } }`. For
  Pipeline B, `name` is the MusicBrainz MBID (a globally-unique UUID), so edges
  reference MBIDs directly.
- **Edge file** — JSON array of `{ "rel", "from", "to", … }`.

These are the publish-ready artifacts.

---

## 5. Generate Fangorn schemas + bundle

The node/edge files describe a graph but not its *shape*. `quickbeam data schemagen`
infers a Fangorn `SchemaDefinition` for each node type and a **bundle** shape that
joins them, ready to register with the Fangorn SDK. This is the
relational-DB-→-Fangorn-schema bridge: point it at any extracted graph and it
derives the schemas + bundle.

```sh
quickbeam data schemagen --volume 1
```

Reads `./stage_volumes/volume_1_*.json` and writes definitions to
`./stage_volumes/schemas/`:

- `fangorn.mb.<type>.v1.json` — one resolver schema per node type present (Artist,
  ReleaseGroup, Release, Recording, Work, Area, Place, Event, Instrument — whatever
  you exported)
- `fangorn.mb.creativecore.v1.json` — the bundle shape: `nodes` (type → schema
  name) + `edges` (`{rel, from, to, min}`)
- `fangorn_schemas.json` — everything consolidated, in registration order (node
  schemas first, then the bundle)

How it works: it samples each node file to infer every field's `@type`, and scans
the edge file for the distinct `(rel, fromType, toType)` triples that become the
bundle's edge shapes.

| Flag | Default | Purpose |
|---|---|---|
| `--input-dir` / `--volume` | `./stage_volumes` / 1 | Where the `volume_<n>_*.json` files are |
| `--out-dir` | `./stage_volumes/schemas` | Where to write the definitions |
| `--prefix` / `--version` | `fangorn.mb` / `v1` | Schema names: `<prefix>.<type>.<version>` |
| `--bundle-name` | `creativecore` | Bundle stem → `<prefix>.<bundle-name>.<version>` |
| `--sample` | 20000 | Records sampled per type for type inference |
| `--edge-scan` | 0 (all) | Edges to scan for the bundle shape |
| `--all-strings` | off | Declare every field `{"@type":"string"}` |

> **Field `@type` note.** The SDK docs confirm `"string"` and `"handle"`. The
> generator infers `"number"`/`"boolean"` for scalar fields and stringifies
> collections; if your SDK build only accepts `string`/`handle`, pass
> `--all-strings`. The mapping lives in the `T_*` / `ARRAY_TYPE` constants at the
> top of `quickbeam/pipelines/fangorn_schema.py`.

> **Edge cardinality** defaults to `min: 0` (permissive — a publish is never
> rejected for a missing relationship). Tighten by hand in the bundle file where a
> relationship is genuinely required (e.g. every Recording must have an artist).

### Registering & publishing

`schemagen` only writes definitions — registration and the on-chain commit are
done by **`src/test/publish_bundle.ts`** in the Fangorn repo. It registers the
node schemas + bundle (idempotently, reading `schemas/fangorn_schemas.json`),
streams every `volume_*.json` node and edge file straight into `publishBundle`,
and commits **the entire graph in ONE transaction**.

> **Why one tx.** A bundle commitment is a single merkle root over many leaves;
> `publishBundle` chunks nodes (per type) + edges into ~`--chunk-size` leaves
> under one root → one `dataSourceRegistry.publish` → one tx. The old single
> giant edge chunk hit V8's ~512 MB `JSON.stringify` wall; chunking removed that
> without changing the tx count (still one). Memory stays ~one chunk at a time.

```sh
# in the Fangorn repo; needs DELEGATOR_ETH_PRIVATE_KEY, PINATA_JWT,
# PINATA_GATEWAY, CHAIN_NAME[, RPC_URL] (env or ~/.fangorn/config.json)
dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir /home/driemworks/fangorn/embeddings/stage_volumes \
  --volume 1
```

On success it prints the registered bundle `schemaId` and the exact
`quickbeam build --bundle "<name>=<id>" --root-type …` line to run next.

> ⚠️ **Do NOT use `--limit` for a real publish — it truncates EDGES, not just
> nodes, and produces a disconnected graph.** `--limit N` streams only the *first
> N* records of every file, including `volume_<n>_edges.json`. That edge file is
> ordered by relationship type, so the first edges are all one homogeneous kind
> (e.g. `Recording→Recording` "music video"). A `--limit 5000` publish therefore
> commits 5000 edges that touch almost none of the published nodes → at embed
> time the projection walk finds **zero neighbors**, so every record comes out
> with **no relationship/list fields** (the schema browser shows isolated nodes
> with no links). `--limit` is **only** for a cheap "does it run" dry run.
> For any browsable graph, omit `--limit` (publish everything) or use
> `--shard-roots` below, which edge-joins each shard so it stays connected.

**Node/edge → publishBundle mapping** (done by the script): the `volume_*.json`
files are `{name, fields}` and `{rel, from, to, …}`; a node becomes
`{ id: name, type: fields.entityType, fields }` and an edge becomes
`{ rel, from, to }`.

Key flags:

| Flag | Default | Purpose |
|---|---|---|
| `--input-dir <path>` | `./stage_volumes` | Dir with `volume_<n>_*.json` + `schemas/` |
| `--volume <n>` | 1 | Which volume to publish |
| `--chunk-size <n>` | 1000 | Entries per merkle leaf |
| `--concurrency <n>` | 4 | Parallel chunk uploads (keep low on a modest uplink) |
| `--limit <n>` | 0 (all) | **Dry-run only.** First N records *per file, edges included* → disconnected graph. Never use for a real publish (see warning above) |
| `--skip-register` | off | Resolve existing schema ids only; don't register missing ones |
| `--dataset <name>` | `ds.<bundle>` | Dataset name for the commitment |

#### Laptop-buildable shards (`--shard-roots`)

One-tx publish is ideal, but the **consumer** (`quickbeam build`) loads one
manifest's nodes into RAM to fold neighbors per root. For a graph too big to hold
at once on a small laptop, run in **sharded mode**: each shard is a
self-contained manifest (roots + their neighbor nodes + edges) and is **its own
one-tx `publishBundle`**, built with bounded RAM via a GNU `sort` merge and
resumable via a ledger.

This is the **recommended way to publish a browsable subset on a laptop**: unlike
`--limit` (which truncates edges into a disconnected mess), sharded mode
sort/merge-joins each root with its *actual* edges and pulls in the real neighbor
nodes, so projections come out **connected** (events link to artists, etc.). Cap
the size with `--shard-roots` rather than `--limit`.

```sh
dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
  --input-dir /home/driemworks/fangorn/embeddings/stage_volumes \
  --shard-roots 200000 --root-type Recording \
  --sort-mem 256M --index-dir tmp
```

| Flag | Default | Purpose |
|---|---|---|
| `--shard-roots <n>` | 0 (off) | Roots per shard; `>0` enables sharded mode (one tx **per shard**) |
| `--root-type <type>` | Recording | Which node type seeds the shards |
| `--sort-mem <size>` | 256M | GNU sort buffer (small = laptop-safe; spills to disk) |
| `--sort-parallel <n>` | CPU count | GNU sort threads |
| `--index-dir <path>` | tmp | Work dir for sort spill (needs ~2× edge size free) |
| `--ledger <path>` | `tmp/bundle-<dataset>.json` | Resume ledger (re-run skips done shards) |
| `--max-retries <n>` | 8 | Retry attempts per shard on transient errors |

---

## 6. Embed & serve (downstream)

`quickbeam build` does **not** read the local `volume_*.json` files directly — it
pulls the published manifest(s) from the Fangorn subgraph and resolves node/edge
chunks from IPFS. So after §5 has registered + published:

**Embed** with the bundle/edge-walk builder, which walks each root node's
outgoing edges, folds neighbor fields into the document, and writes vectors (plus
an optional 2D UMAP catalog map) to Qdrant:

```sh
# Start Qdrant (first time). If it already exists but stopped, use: docker start qdrant-core
docker run -d --name qdrant-core -p 6333:6333 -p 6334:6334 qdrant/qdrant

quickbeam build \
  --bundle "<bundle-schema-name>=<0x-schema-id>" \
  --root-profile track --root-profile artist --root-profile place --root-profile event \
  --graph-api-key <key> \
  --ipfs-gateway https://<your-gateway>.mypinata.cloud/ipfs \
  --dim 256 --umap --reset
```

Use the `<name>=<id>` printed by `publish_bundle.ts`. Pass one `--root-profile`
per view you want embedded (each becomes a filterable `entityType` in the browser).

> The Qdrant container can be killed by laptop memory pressure mid-run
> (`docker ps -a` shows `Exited (255)`). Just `docker start qdrant-core` and
> re-run `quickbeam build` — it resumes from its checkpoint.

**Root profiles — the graph as source of truth.** The bundle is a typed graph;
a *profile* projects it from a chosen root type into a distinct document by
walking up to `max_depth` hops and folding the neighbor entities it cares about
into grouped label lists. The same graph yields a Track view, an Artist view, a
Place view, … — **each a separate embedding** carrying an `entityType` you can
filter on at search time. Built-in profiles live in `ROOT_PROFILES` in
`quickbeam/ingest/graph/projection.py`:

| Profile | Root type | Depth | Folds in |
|---|---|---|---|
| `track` / `recording` | Recording | 2 | Artist, Work, Release(Group), Place, Event, Area |
| `artist` | Artist | 2 | Recording, Release(Group), Work, Place, Event, Area |
| `release` | Release | 2 | Artist, Recording, ReleaseGroup, Work |
| `place` | Place | 3 | Artist, Recording, Event, Area |
| `event` | Event | 2 | Artist, Recording, Place, Area |
| `work` | Work | 2 | Artist, Recording, Release |

Pass `--root-profile` once per view you want (repeatable). Add a new semantic
view by editing `ROOT_PROFILES` or supplying `--profiles-file <json>` (merged over
the built-ins) — no change to the graph or the publish step.

| Flag | Default | Purpose |
|---|---|---|
| `--root-profile <name>` | (none) | Projection(s) to emit; repeatable |
| `--profiles-file <json>` | — | Custom/override profiles merged over `ROOT_PROFILES` |
| `--max-depth N` | 2 | Default walk depth for profiles that don't set one |
| `--label-cap N` | 50 | Max neighbor labels per relation group |
| `--node-cap N` | 2000 | Max nodes a single root's walk visits (cost bound) |
| `--root-type <type>` | Track | **Legacy** single-projection root (one-hop field fold) used only when no `--root-profile` is given |

> Omitting `--root-profile` falls back to the legacy single `--root-type`
> projection (one-hop neighbor *field* fold), preserving the original behavior.

The build is resumable via `--checkpoint-file`.

3. **Serve / search**: `quickbeam serve …` then query, or export a portable
   snapshot. See the README for `serve`, `watch`, `export`, and snapshot pinning.

---

## 7. Schema browser (`examples/`)

A wiki-style web UI to explore the embedded graph in Qdrant — every entity is a
page; fields and related entities are clickable. It lives in `examples/` (Vite +
React + TypeScript) and talks to Qdrant directly via a dev proxy.

```sh
cd /home/driemworks/fangorn/embeddings/examples
npm install
npm run dev          # → http://localhost:5173  (needs Qdrant on :6333 with the `fangorn` collection)
```

How it works:
- Reads the live `fangorn` collection over a Vite dev proxy (`/qdrant/*` →
  `http://localhost:6333/*`, set in `vite.config.ts` — avoids CORS). The proxy is
  **dev-only**; `npm run preview`/prod needs a real Qdrant URL.
- Renders typed fields from the JSON schemas snapshotted into
  `examples/public/schemas/`. **Re-copy them whenever you regenerate schemas:**
  ```sh
  cp /home/driemworks/fangorn/embeddings/stage_volumes/schemas/*.json \
     /home/driemworks/fangorn/embeddings/examples/public/schemas/
  ```
- Navigation model (honest about the data): Qdrant payloads carry **no node-id
  edges**, so "links" are either a *search* (clicking `byArtist` or an item in a
  projected list like `artists[]`/`events[]` runs a query) or *semantic
  neighbors* (vector recommend). The UI marks these distinctly (`⌕` search vs a
  hard edge vs `↗` external MusicBrainz link).

> The relationship/list fields the browser navigates only exist if the data was
> published **connected** (no `--limit`) and embedded with `--root-profile`s. If
> entities show up isolated with no links, you almost certainly published with
> `--limit` — see §5 and the troubleshooting note below.

Design/IA specs the build follows: `examples/docs/DESIGN.md` (visual system,
components) and `examples/docs/LANGUAGE.md` (entity glossary, field labels,
relationship phrasing).

---

## 8. Troubleshooting

- **`ReadError: empty file` (Pipeline A)** — corrupt/zero-holed cached dump from an
  interrupted parallel download. Re-run with `--force-download`.
- **`createdb.sh: The dump 'X' is missing`** — you didn't trim `DUMP_FILES` (§3.2a),
  or a dump isn't in the `dbdump` volume. Re-copy (§3.2c).
- **Ingester can't connect** — confirm the DB is up (`docker compose ps`), the
  `5432:5432` port override is in place, and the DSN points at `musicbrainz_db`.
- **A column/table error from `mbpg`** — the music five use the `SQL_*` constants
  near the top of `quickbeam/pipelines/mb_pg.py`; the introspecting entities
  (area/place/event/instrument) build SQL via `_mb_core_sql`. If a future schema
  rev renames something, that's where to look.
- **Disk pressure during import** — the Postgres data lands in the
  `musicbrainz-docker_pgdata` Docker volume (under Docker's data root, usually on
  `/`). Ensure that filesystem has ~120 GB free.
- **`schemagen` warns "endpoint type not among node schemas, skipping"** — an edge
  references a node type whose file isn't present (e.g. you exported only a subset
  via `--entities`). Export the missing entities, or accept that those edges are
  dropped from the bundle shape.
- **Fangorn `schema.register` rejects an `@type`** — your SDK build only accepts
  `string`/`handle`. Re-run `schemagen --all-strings`.
- **Browser shows isolated entities — no relationships/links (e.g. Events have no
  Artists)** — you published with `--limit`, which truncates the *edge* file to its
  first N records (all one homogeneous relationship type), disconnecting it from
  the node subset. The source data is fine (the `l_*` tables have hundreds of
  thousands of these edges). Re-publish without `--limit`, or with `--shard-roots`
  (§5), then re-run `quickbeam build`. Verify edges exist:
  `grep -c '"toType": "Event"' stage_volumes/volume_1_edges.json`.
- **Qdrant container `Exited (255)` mid-build** — laptop memory pressure killed it.
  `docker start qdrant-core` and re-run `quickbeam build` (it resumes from its
  checkpoint). The schema browser also shows a connection error until it's back up.
