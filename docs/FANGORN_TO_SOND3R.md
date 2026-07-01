# Fangorn → sond3r: the automated business-profile pipeline

A business owner writes a profile in the sond3r UI. It gets registered on-chain, the
watcher auto-embeds it, and it's delivered to every client as a **delta shard** — a
few hundred bytes — with no full snapshot re-download.

```
sond3r UI  ──publish──►  IPFS/Pinata + ManifestPublished (on-chain)
   (fangorn TS lib: PublisherRole.publish + RecordSetBuilder)
        │
        ▼
   Subgraph indexes ManifestPublished
        │
        ▼
   quickbeam watch  ── polls subgraph, fetches IPFS, embeds ──►  Qdrant "fangorn"
        │  (same cycle, if --cdn-dir/--cdn-domain set)
        ▼
   cdn.append_domain  ──►  cdn/places/shard-NNNN-<hash>.ndjson.gz  (delta only)
                           cdn/places/manifest.json  (mutable, no-cache: +1 shard)
        │
        ▼
   sond3r loads catalog → manifest → shards.  Old shards are immutable HTTP cache
   hits; only the new delta shard is downloaded.
```

## Why delta shards, not a full re-bake / not gossipsub

`cdn bake` re-scrolls the whole collection and rewrites the domain's shard, so one new
profile would mint a fresh content-hash shard containing **everything** — clients
re-download the full snapshot. `cdn append` writes only the un-baked points as an
additional content-addressed shard and appends it to the manifest. Existing shards are
immutable and untouched, so a returning client pulls only the delta.

This reuses the entire existing CDN over plain HTTP. gossipsub/libp2p pubsub is the
eventual real-time-push story, but it's weeks of relay/bootstrap/transport infra to
replicate what a ~2 KB manifest re-fetch does here. Kept out of this experiment.

## Run the loop

```sh
# 0. one-time: bake the base domain (already done → cdn/places)
quickbeam cdn bake --domain places

# 1. run the watcher with live delivery on. New BusinessProfile records embed and
#    ship as a delta shard into cdn/places on the same cycle.
quickbeam watch \
  --bundle fangorn=0x<SCHEMA_ID> \
  --root-type BusinessProfile \
  --dataset BusinessProfile \
  --cdn-dir ./cdn --cdn-domain places

# 2. serve the CDN
quickbeam cdn serve --cdn-dir ./cdn --cors

# manual delta (no watcher) — scroll Qdrant for un-baked points and ship them:
quickbeam cdn append --domain places
```

`append` is idempotent: it reads existing shards to skip already-delivered ids, so
re-running is safe.

## Watcher: bundle vs view, and why this pipeline uses `--bundle`

The watcher takes exactly one of `--bundle` (one publisher's graph, edge-walk join) or
`--view` (fuses several sources + linksets via union-find — `build_view_joined_data`).
Both load projection profiles (`--root-type` / `--root-profile` / `--profiles-file`),
same as `build`. (Historically the watcher passed `--root-type` where the join expects
a *profiles* list — a stale-signature bug; now fixed.)

This pipeline runs **`--bundle`** by design (decision: *associate, don't merge*). A
`BusinessProfile` is its own node/`entityType`; it is NOT fused into the Google listing.
That keeps delivery a clean **append** (the profile is net-new; the place row is
untouched) — view fusion re-fuses wholesale and *mutates* existing rows, which the
append-only shard model can't supersede. The profile and place are associated by their
shared `placeId`; the client resolves that to show a "claimed by owner" badge (the same
child→parent roll-up pattern reviews use), so no server-side merge is needed.

`--view` remains available for when cross-source identity collapse into single cards is
actually wanted; it would need a re-bake cadence or row-supersession (content-addressed
rows + client dedup-by-id) to coexist with delta delivery.

## The BusinessProfile schema

`schemas/business-profile.json` extends `place.json` (name / description / encrypted
address) with owner-authored fields and the fields the `places` role_map renders
(`title`, `placeId`, `locality`, `primaryType`) so claimed profiles render alongside
Google places. `BusinessProfile` is registered in `domains.json` (places filter +
presentation: ✔️ "Claimed Business").

Register + publish (fangorn CLI):

```sh
fangorn schema register fangorn.places.businessprofile.v0 -e
```

`entityType` is set at embed time from the record's root type (`embeddings.py:1085`),
so the RecordSet must be published with root type `BusinessProfile`.

## The submit form — a generic schema-browser shell (examples app)

The write UI lives in the `examples` Vite app, built as a *generic* schema browser
rather than a one-off form:

- `src/lib/schemas.ts` — the `PublishableSchema` registry. Each entry describes a
  publishable schema (id, on-chain schemaId, rootType, fields, prefill). Add an entry
  → the app grows a new create form + browser card. Seeded with `BusinessProfile`.
- `src/components/SchemaForm.tsx` — renders inputs from any schema's field defs,
  validates, and calls the publish seam. No per-schema code.
- `src/pages/Create.tsx` — the shell: `/create` lists schemas, `/create/:schemaId`
  opens a (query-prefillable) form. Reachable from the top bar "✎ Publish".
- `ProfileOwnership` "Claim this profile" opens the `BusinessProfile` form inline,
  pre-filled from the listing being claimed.

### Publish seam + the endpoint contract

`src/lib/publish.ts` keeps Pinata keys and the fangorn pack **out of the browser**. It
POSTs plain fields to `VITE_PUBLISH_URL` when set, else saves an honest local draft
(mirrors `claims.ts`). The endpoint is a thin node service running
`@fangorn-network/sdk` `PublisherRole` server-side:

```
POST {VITE_PUBLISH_URL}/publish
  { schemaName, schemaId, rootType, owner, fields }   →   { manifestUri, txHash }
```

It encrypts encrypted fields, packs a one-record `RecordSetBuilder` under
`schemaName` (root type `BusinessProfile`), pins to IPFS, and emits
`ManifestPublished`. The watcher (above) does the rest. Building this endpoint is the
one remaining piece to close the loop end-to-end.

## Live-without-reload (nice-to-have)

`shards.ts` already picks up appended shards on reload. For live update without a
reload, add a manifest poll: re-fetch `manifest.json` (no-cache) every N seconds; when
`shards.length` grows, fetch the new shard and merge its points into the in-memory
`points` array (dedupe by deterministic id). This is the seam where gossipsub could
later replace polling.
