# Semantic CDN: local-first discovery

A guide to distributing the embedded graph as **static, pullable knowledge** so that
discovery happens on the user's machine instead of on the server.

---

## Why

Fangorn's thesis is **"knowledge is public, intent is private."**

The search server (`quickbeam serve`) runs queries **server-side**: the client sends a
query, the node embeds it, searches Qdrant, and returns hits. That means the node sees
**every query vector** — and a semantic query *is* intent (interests, health concerns,
purchasing intent, politics, all inferable from embeddings alone). A web-of-embeddings
that centralizes query execution becomes a surveillance system more powerful than the
ones it replaces.

The Semantic CDN inverts the relationship:

```
   server-side search                 Semantic CDN (local-first)
   ──────────────────                 ──────────────────────────
   user → query → NODE → results      NODE → knowledge → user → (local) query → results
          (node sees intent)                 (node sees only which domain was pulled)
```

The node distributes **public** knowledge (schemas, bundles, embeddings — all already
public on IPFS / the subgraph). The user pulls a slice into their **own** local Qdrant
and queries it offline. The network is left structurally unable to accumulate a
behavioral profile. This mirrors BitTorrent: move content toward users instead of
routing every request through central infrastructure.

---

## The shape of it

Three commands, two roles:

| Role | Command | What it does |
|---|---|---|
| **Operator** | `quickbeam cdn bake` | Qdrant collection → immutable, content-addressed shard files (one *domain* per declared filter) |
| **Operator** | `quickbeam cdn serve` | Serve the baked directory as static, range-resumable HTTP |
| **User** | `quickbeam pull <domain>` | Download + verify a domain's shards, load them into a **local** Qdrant collection |
| **User** | `quickbeam serve --collection <domain>` | Query the pulled collection locally — the CDN sees nothing |

A **domain** is operator-declared: a named filter over the source collection. The first
implementation is deliberately single-node — no federation, no mixnets — to validate the
privacy model before introducing distributed-systems concerns.

> **Prerequisite.** A populated Qdrant collection (default `fangorn`), produced by
> `quickbeam build` against a published bundle. See [REBUILD_FROM_ZERO.md](REBUILD_FROM_ZERO.md)
> for how to get there. The CDN distributes what `build` produced; it does not embed.

---

## 1. Declare domains (`domains.json`)

A domain maps a name to a description and a **filter** over the collection's payloads.
Supported filter keys: `entityType` (list) and `owner` (list); each is a Qdrant
`MatchAny`, and multiple keys are AND-ed. An empty/missing filter selects everything.

```json
{
  "domains": {
    "music": {
      "description": "MusicBrainz creative core — recordings & artists",
      "filter": { "entityType": ["Recording", "Artist"] },
      "bundle_schema": "stage_volumes/schemas/fangorn.mb.creativecore.v3.json",
      "presentation": {
        "types": { "Artist": { "icon": "🎤", "accent": "#f7768e" } },
        "fieldLabels": { "byArtist": "By", "durationMs": "Length" },
        "externalUrl": { "Artist": "https://musicbrainz.org/artist/{mbid}" }
      }
    },
    "venues": {
      "description": "Places & events",
      "filter": { "entityType": ["Place", "Event"] }
    }
  }
}
```

Two optional, per-domain keys make a baked domain **self-describing for offline,
schema-agnostic clients** (so the renderer needs no per-domain code):

- **`bundle_schema`** — path to a Fangorn bundle schema JSON (or a bare
  `{nodes, edges}` block). Its type + relationship vocabulary is copied into the
  manifest's `bundle` field so a client can render typed Connections without the live
  schema registry. Omit it and `bundle` is simply absent.
- **`presentation`** — an optional overlay (icons / accent colors / `fieldLabels` /
  `externalUrl` templates) copied verbatim into the manifest. It is pure polish: with
  no overlay the client falls back to inferred labels + generic visuals.

Regardless of these, every manifest also gets an inferred **`role_map`**
(title/subtitle/tags/temporal/spatial/… — the same inference [`server.py`] does at
runtime, but over this domain's own sample) and an **`entity_types`** vocabulary with
per-type counts. Together these let a pulled domain drive a generic browser with zero
hardcoding.

Domains can overlap, slice by publisher (`"owner": ["0x…"]`), or mirror your
[root profiles](../quickbeam/ingest/graph/projection.py) (track / artist / place / event …). The
operator decides the granularity — fine-grained domains let users pull only what they
want; a single broad domain ships the whole corpus at once.

---

## 2. Bake (`quickbeam cdn bake`)

Reads the source collection once and writes immutable shards per domain.

```sh
quickbeam cdn bake \
  --config domains.json \
  --cdn-dir ./cdn \
  --collection fangorn \
  --shard-size 50000          # points per shard file
```

Output layout:

```
cdn/
  catalog.json                 # [{name, description, count, dim, bytes, shard_count, entity_types}]
  music/
    manifest.json              # {name, count, dim, model, distance, filter, role_map, entity_types,
                               #  shards:[{file,count,bytes,sha256}], bundle?, presentation?}
    shard-0000-9f3a1c2b7e04.ndjson.gz   # gzipped NDJSON; filename embeds the sha256
    shard-0001-2d77ab90f1c5.ndjson.gz   #   prefix → a re-bake mints new urls
  venues/
    manifest.json
    shard-0000-....ndjson.gz
```

Each shard line reuses the existing `/bundle/export` shape verbatim
(`{track_id, fields, embedding, owner, meta}`), so the CDN, `pull`, and the server's
`/bundle/import` all agree on the wire format. The **sha256 in each manifest is the
hash of the gzipped file on disk** — exactly what the client re-computes after download,
so corruption or truncation is caught.

Bake is **atomic per domain** (writes to `<domain>.tmp`, then `os.replace`) and
**incremental across domains**: re-baking one domain (`--domain music`) preserves the
others' catalog entries. `dim` and `distance` are read from the live collection;
`--embedding-model` is recorded in the manifest because Qdrant does not store it.

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--config` | `domains.json` | Domain definitions |
| `--cdn-dir` | `./cdn` | Output directory |
| `--collection` | `fangorn` | Source Qdrant collection |
| `--domain NAME` | all | Bake just one domain (others' catalog entries preserved) |
| `--shard-size N` | 50000 | Points per shard — smaller = more parallel/resumable downloads |
| `--limit N` | 0 | Cap total points baked per domain (0 = all). Bake a small, browser-friendly snapshot |
| `--scroll-batch N` | 2000 | Qdrant scroll page size |
| `--qdrant-url`/`-api-key` | — | Bake from Qdrant Cloud instead of local |

---

## 3. Serve (`quickbeam cdn serve`)

A separate, minimal FastAPI app that serves the baked directory as **static files** —
no Qdrant connection, no embedding model, no query path. That is the whole point: there
is nothing here that *could* observe a query.

```sh
quickbeam cdn serve --cdn-dir ./cdn --port 8090
# add --cors for browser-based pulls
```

Routes:

| Route | Returns |
|---|---|
| `GET /catalog` | the top-level catalog (domains + sizes) — `Cache-Control: no-cache` (mutable pointer; revalidated via ETag) |
| `GET /domains/{name}/manifest` | a domain's shard index (sha256 per shard) — `Cache-Control: no-cache` (mutable pointer) |
| `GET /domains/{name}/shards/{file}` | raw shard bytes — **HTTP Range supported (206)**, `Cache-Control: immutable`, ETag. Filenames are sha256-stamped, so `immutable` is safe across re-bakes |
| `GET /health` | liveness + whether a catalog is present |

Because shards are immutable and content-addressed, you can put any dumb cache in front
(nginx, Cloudflare) or pin the directory to IPFS later with zero code change — the
sha256 manifest already gives you content addressing.

```sh
# sanity checks
curl -s localhost:8090/catalog | jq '.domains[].name'
curl -s -D - -o /dev/null -r 0-1023 \
  localhost:8090/domains/music/shards/shard-0000.ndjson.gz | grep -i '206\|content-range'
```

---

## 4. Pull (`quickbeam pull`)

The user-facing half. Downloads a domain's shards, **verifies each against its sha256**,
and loads them into a **local** Qdrant collection.

```sh
quickbeam pull music \
  --cdn-url http://localhost:8090 \
  --collection music_local \
  --concurrency 4 \
  --reset
```

- **Resumable.** A partial download lives in `<file>.part`; a retry continues it with an
  HTTP `Range` request rather than restarting. A verified `<file>` in the cache is reused
  without touching the network.
- **Idempotent.** Points are upserted with deterministic ids (`uuid5` of `track_id`,
  matching the builder/server), so re-pulling overwrites the same points instead of
  duplicating — re-running a finished pull is a no-op on the count.
- **Verified.** A sha256 mismatch fails the shard (with retries) rather than loading
  corrupt vectors.

`--download-only` fetches + verifies the shards into the cache without loading Qdrant
(useful for pre-staging or mirroring). The collection is created with the dim + distance
from the manifest.

| Flag | Default | Purpose |
|---|---|---|
| `domain` | required | Which domain to pull (positional) |
| `--cdn-url` | `http://localhost:8090` | CDN base URL |
| `--collection` | = domain | Local collection to load into |
| `--cache-dir` | `./db/cdn_cache` | Where shards are cached |
| `--concurrency` | 4 | Parallel shard downloads |
| `--reset` | off | Recreate the local collection first |
| `--download-only` | off | Verify shards but don't load |
| `--qdrant-*` | local | Target Qdrant for the local collection |

---

## 5. Query locally

Once pulled, the domain is just a local Qdrant collection — existing tooling works
unchanged:

```sh
quickbeam serve --collection music_local --port 8080
curl 'localhost:8080/search?q=berlin techno&n_results=5'
```

The query is embedded and searched **on the user's machine**. The CDN logged only that
`music` was downloaded — not what was searched for. That is the milestone: a complete
discovery loop where behavioral sovereignty is preserved on a single node.

---

## End-to-end (copy/paste)

```sh
# ── operator ──────────────────────────────────────────────────────────────
docker start qdrant-core            # a populated `fangorn` collection must exist
quickbeam cdn bake  --config domains.json --cdn-dir ./cdn --shard-size 50000
quickbeam cdn serve --cdn-dir ./cdn --port 8090 &

# ── user ──────────────────────────────────────────────────────────────────
quickbeam pull music --cdn-url http://localhost:8090 --collection music_local --reset
quickbeam serve --collection music_local --port 8080
curl 'localhost:8080/search?q=ambient techno'
```

---

## Design notes & next steps

- **Why pre-baked, not live-streamed.** Serving by scrolling Qdrant per request is not a
  CDN — it isn't cacheable, can't be mirrored or pinned, re-scrolls per client, and a
  multi-hour pull spans live mutations (a torn snapshot). Pre-baked shards are an
  immutable, point-in-time, verifiable cut: cacheable, resumable, IPFS-ready. The live
  path still exists as `/bundle/export` on the search server if you ever want freshness
  over cacheability.
- **Versioning.** Treat a bake as a release. When the corpus changes, re-bake to mint a
  new immutable set; clients pull only the shards they don't already have (by sha256).
  Incremental updates from `quickbeam watch` become appended `shard-NNNN` files.
- **Scaling — the real lever is quantization.** At dim 256 float32 a vector is ~1 KB, so
  10M points ≈ 10 GB. Binary quantization at 256-d is ~32 bytes/vector → 10M ≈ 320 MB,
  100M ≈ 3 GB — i.e. a useful domain fits comfortably on a laptop. A `--quantize` option
  in `bake` is the planned next step. A single domain is laptop-local today; the *whole
  web* is explicitly out of scope for v1.
- **Payments / privacy hardening (later).** x402 gating on shard downloads is a bolt-on
  (charge once per domain, not per query — the opposite of surveillance-search). IPFS
  pinning is a drop-in since shards are already sha256-addressed. Mixnets / PIR for the
  inevitable "not available locally" fallback are deliberately deferred until the
  local-first model is validated.
