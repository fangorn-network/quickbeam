# Pipeline stages: bronze → silver → gold

One mental model for the whole pipeline. Every command you run lives in one of three
stages, and data only ever moves **forward**. If you're ever unsure "where am I / what
is this file," find it on this page.

```
  BRONZE                 SILVER                                  GOLD
  raw, as-scraped        cleaned · typed · deduped · fused       serving-ready
 ┌──────────────┐      ┌───────────────────────────────────┐   ┌──────────────────┐
 │ places_raw   │      │ volume_*.json   (typed nodes/edges)│   │ published        │
 │ events_raw   │ ───► │   per source, schema-conformed     │──►│ bundles / view   │──► Qdrant
 │ (or *.jsonl) │      │ + View fusion (one entity per thing)│   │ + baked CDN shard│    + CDN
 └──────────────┘      └───────────────────────────────────┘   └──────────────────┘
   scrape once,          shape + fuse (free, repeatable)          publish + embed
   costs money/quota
```

The rule that keeps it sane: **each stage is rebuildable from the one before it.**
Re-run silver from bronze for free; re-run gold from silver. Only bronze touches the
network (and your wallet).

---

## 🥉 Bronze — raw capture

Verbatim source payloads, stored exactly as fetched. Immutable, append-only,
idempotent (re-fetching upserts by a natural key, never duplicates).

| You run | Produces | Key |
|---------|----------|-----|
| `quickbeam data places-fetch …` | `places_raw` rows (or `--raw-out *.jsonl`) | `place_id` |
| `quickbeam data events-fetch …` | `events_raw` rows (or `--raw-out *.jsonl`) | `event_key` |
| `quickbeam data osm …` | OSM payloads | osm id |

**Why a separate layer:** scraping is the only step that costs money/quota and hits
rate limits. Capture once; iterate everything downstream for free. This is also where
you keep sources **physically separate** when you don't want them blended (e.g.
tribe-only `--raw-out tribe.jsonl`, so a later shape can't mix it with Eventbrite).

**Promotion gate → silver:** none. Bronze is "whatever the source said."

---

## 🥈 Silver — conformed & fused entities

The canonical layer: clean, typed, deduplicated, and joined so that **one real-world
thing is one entity**. Two sub-steps:

**Silver-A — shape (per source).** Turn raw payloads into typed nodes + edges that
conform to a schema, with a global identity (`@id` + namespaced aliases) stamped on.

| You run | Produces |
|---------|----------|
| `quickbeam data placespg / eventspg / osm …` | `volume_<n>_*.json` typed nodes + edges |
| `quickbeam data schemagen …` | the schema each source conforms to |

**Silver-B — fuse (across sources).** Collapse records that describe the same thing
into one entity. Two mechanisms, same result:

- **Automatic** — records sharing a namespaced alias (`gplace:…`) merge on their own.
- **Explicit** — a **linkset** asserts `sameAs` joins where there's no shared id (see
  [`FUSING_SOURCES.md`](./FUSING_SOURCES.md)). `quickbeam data linkgen` builds one by
  coordinate + name match.

> Today, Silver-B (fusion) is computed **at build time** by the View, not persisted as
> its own artifact. That's fine for now; if rebuilds get expensive or you want to query
> fused entities directly, the next step is to *materialize* this layer. Until then,
> think of the View declaration as the recipe for silver, evaluated lazily.

**Promotion gates → gold:** per-record **schema validation** at publish, the
**schema-drift guard** (rejects data that doesn't match an already-registered schema),
and the linkset **`minConfidence`** floor (drops weak `sameAs` assertions).

---

## 🥇 Gold — serving-ready

The consumption products: immutable, versioned, purpose-built for the app.

| You run | Produces |
|---------|----------|
| `publish_bundle.ts` | a source published as an on-chain **datasource** (one per source) |
| `publish_linkset.ts` | the `sameAs` joins published as a datasource |
| `publish_view.ts` | a **View** = "which sources + linksets to fuse" |
| `quickbeam build --view …` | the embedded vector **shard** (Qdrant) |
| `quickbeam cdn bake / serve` | the **CDN** artifact the demo app loads |

**Why on-chain here, not earlier:** gold is where immutability, provenance, and
versioning matter — each publish is a content-addressed commit. Re-publishing one
source bumps *its* version and nothing else; the View always reads each source's latest.

---

## Reading the rest of the docs through this lens

| Doc | Stages it covers |
|-----|------------------|
| [`LOCAL_DISCOVERY_GUIDE.md`](./LOCAL_DISCOVERY_GUIDE.md) | the full bronze → gold walkthrough, with the "why" |
| [`QUICKSTART.md`](./QUICKSTART.md) | the commands, stage by stage |
| [`FUSING_SOURCES.md`](./FUSING_SOURCES.md) | Silver-B (explicit fusion via linksets) |

**The three publish scripts, by what they promote to gold:**
`publish_bundle.ts` = a source's data · `publish_linkset.ts` = joins between sources ·
`publish_view.ts` = which sources + joins to fuse and serve.

---

## Quick "where am I?" lookup

| If you have… | …it's | Rebuild it from |
|--------------|-------|-----------------|
| `*_raw` table / `*.jsonl` | bronze | re-scrape (costs money) |
| `volume_*.json` | silver-A | bronze (`*pg` shaper) |
| `*_links.json` (from linkgen) | silver-B recipe | silver-A node files |
| a published bundle / linkset / view | gold | silver (publish scripts) |
| the Qdrant shard / CDN dir | gold | the View (`quickbeam build`) |
