# Fusing two data sources into one entity

You have the same real-world things described by **two independent sources** — say a
bar that exists in **Google Places** *and* in **OpenStreetMap**. Published separately,
they show up as **two entries**. This guide makes them **one fused entity** that
carries both sources' fields.

You don't merge the data or reshape it. Each source stays its own independent
publish. You only add a small **linkset** that says "these two are the same thing."

---

## The one idea

A **View** stitches datasources together. It fuses two records into one in exactly
two situations:

| | When | What you do |
|---|---|---|
| **Automatic** | The two records share a **namespaced alias** (same `namespace:value`, e.g. both carry `gplace:ChIJ…`) | Nothing — the View merges them for free. |
| **Explicit** | They share **no** alias (Google uses `gplace:`, OSM uses `osm:`) | Publish a **linkset**: a list of `sameAs` edges that assert the join. |

This guide is the **explicit** case. A linkset is just a published list of edges:

```json
[
  { "from": "gplace:ChIJxxx", "rel": "sameAs", "to": "osm:node/4987275152" },
  …
]
```

`from`/`to` are the **aliases the published nodes already carry** (`<namespace>:<id>`).
`sameAs` tells the View: collapse these two into one entity. You ship the **data**
(each source's bundle) and the **joins** (the linkset) separately.

---

## Recipe: fuse Google Places ⟷ OSM businesses

Assumes both sources are already published as their own datasources (Google places
as `…business.v1`, OSM places as `…osm.business.v1`) and you have a View over them.

### 1. Generate the links (matcher — no ML)

Both sources have coordinates and names, so match on **location + name**. This writes
a linkset JSON; it publishes nothing.

```bash
cd ~/fangorn/embeddings
quickbeam data linkgen \
  --left  stage_volumes/volume_1_businesses.json     --left-alias  gplace:placeId \
  --right stage_volumes/volume_3_osm_businesses.json  --right-alias osm:osmId \
  --radius-m 75 --min-name-sim 0.35 \
  --out biz_links.json
```

- `--left-alias gplace:placeId` = "the left endpoint is `gplace:` + the node's `placeId` field." Same idea for `--right-alias`.
- It prints each match (`'Leif's Cafe' ⟷ 'Leif's Cafe' (1.7m, name 1.0, conf 0.99)`) so you can eyeball quality.
- **Too few matches?** raise `--radius-m` or lower `--min-name-sim`. **False matches?** do the opposite.
- Output is `[{from, rel:"sameAs", to, confidence, evidence}]` — confidence and the match evidence are recorded on every edge.

### 2. Publish the linkset

```bash
cd ~/fangorn/fangorn
pnpm dotenvx run -f .env -- tsx src/test/publish_linkset.ts \
  --name eagleriver.sond3r.com.links.placesXosm.v1 \
  --links ~/fangorn/embeddings/biz_links.json
# → registers the linkset, publishes it, prints its resourceId
```

### 3. Add the linkset to your View, rebuild

A View's inputs are fixed at registration, so adding a linkset means a **new view
version**:

```bash
cd ~/fangorn/fangorn
pnpm dotenvx run -f .env -- tsx src/test/publish_view.ts \
  --name eagleriver.sond3r.com.localview.v2 \
  --source-bundle eagleriver.sond3r.com.osm.placecore.v1 \
  --source-bundle eagleriver.sond3r.com.localcore.v1 \
  --linkset-name eagleriver.sond3r.com.links.placesXosm.v1
# → 0x<viewIdV2>

quickbeam build --view "eagleriver.sond3r.com.localview.v2=0x<viewIdV2>" \
  --root-profile business --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

Now each matched pair is **one** business carrying Google's fields (rating, hours,
reviews) **and** OSM's. Unmatched businesses from either source stand alone.

> **Confidence floor (optional).** A View can ignore weak links: register it with a
> trust policy `{"minConfidence": 0.6}` (`publish_view.ts --trust '{"minConfidence":0.6}'`)
> and the build drops any `sameAs` below that score.

---

## How it works (one paragraph)

The View loads every source's nodes into one graph and runs a **union-find**: records
that share an alias merge automatically, and each `sameAs` link from the linkset adds
another merge. So identity-fusion and linkset-fusion feed the *same* mechanism — the
linkset is just manually-supplied evidence for joins the data couldn't make on its own.
The matcher (`linkgen`) is deterministic (coordinate + name), so re-running gives the
same links; regenerate and re-publish whenever either source changes.

---

## Cheat sheet

| Step | Command | Publishes? |
|------|---------|-----------|
| Match two sources → links file | `quickbeam data linkgen …` | no |
| Publish the linkset | `tsx src/test/publish_linkset.ts --name … --links …` | yes |
| Attach to a View + rebuild | `tsx src/test/publish_view.ts … --linkset-name …` then `quickbeam build --view …` | yes |

The three publish scripts are siblings: **`publish_bundle.ts`** (a source's data),
**`publish_linkset.ts`** (joins between sources), **`publish_view.ts`** (which sources
+ linksets to fuse).
