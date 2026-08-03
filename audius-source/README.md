# quickbeam-audius

A pluggable quickbeam `Source` that turns **Audius** into **two sovereign Fangorn
graphs joined by a linkset**.

Audius' premise is that artists publish their own music. So this doesn't build one
catalog graph — it builds the split that premise implies:

| Graph | Published by | Contains |
|---|---|---|
| **A — the platform** | wallet A, namespace `audius` | The trending + underground slice, with the focus artist's catalog **removed**. Keeps a thin `artistref` stub in its place. |
| **B — the artist** | wallet B, namespace `<artist>` | The focus artist's full catalog, profile, albums and playlists. |
| **the linkset** | nobody — it's data | Every edge whose endpoints land on opposite sides. Installed with `quickbeam cdn edges`. |

The platform's graph has a hole shaped like the artist. The artist's graph fills it.
Neither holds the other's data, both settle to their own on-chain root, and the two
read as one searchable graph in the browser.

Both graphs come out of **the same `build_graph`**, so they are schema-identical by
construction — a second source package would have guaranteed drift.

## Install

```sh
pip install -e ./audius-source          # into the same env as quickbeam
quickbeam data audius --help            # the entry point makes the verb appear
```

## The relations

16 relation types, which is the point — a graph this dense is what makes traversal
worth demoing.

| Relation | From → To | Where it comes from |
|---|---|---|
| `created` | Artist → Track | `users/{id}/tracks` |
| `released` | Artist → Playlist | `users/{id}/albums` |
| `curated` | Artist → Playlist | `users/{id}/playlists` |
| `follows` | Artist → Artist | `users/{id}/following` |
| `relatedTo` | Artist → Artist | `users/{id}/related` (Audius' own similarity) |
| `supports` | Artist → Artist | `users/{id}/supporting` — **carries the $AUDIO tip amount** |
| `reposted` | Artist → Track/Playlist | `users/{id}/reposts` |
| `favorited` | Artist → Track | `users/{id}/favorites` |
| `worksIn` | Artist → Genre | derived |
| `contains` | Playlist → Track | `playlists/{id}/tracks` |
| `remixOf` | Track → Track | `tracks/{id}/remixing` |
| `hasStem` | Track → Track | `tracks/{id}/stems` |
| `inGenre` | Track → Genre | derived from the track's own fields |
| `hasMood` | Track → Mood | derived |
| `taggedWith` | Track → Tag | derived |
| `sameAs` | Artist → Artist | synthesized — the linkset's identity claim |

## Run it

```sh
# 1. Crawl once. Everything downstream reads this cache — no more network.
quickbeam data audius --dry-run --side all --cache-file ./audius-build/audius_cache.json

# 2. Stage both graphs FROM THAT CACHE (no network, seconds).
quickbeam data audius --side A --cache-file ./audius-build/audius_cache.json \
  --output-dir ./audius-build/stage --volume 1
quickbeam data audius --side B --cache-file ./audius-build/audius_cache.json \
  --output-dir ./audius-build/stage --volume 2

# 3. Embed both into one collection, badged by publisher.
quickbeam data prebake --input-dir ./audius-build/stage --volume 1 \
  --collection audius --dim 256 --owner 0xPLATFORM…
quickbeam data prebake --input-dir ./audius-build/stage --volume 2 \
  --collection audius --dim 256 --owner 0xARTIST…

# 4. The linkset, then bake + install it.
audius-link --cache-file ./audius-build/audius_cache.json --out ./audius-build/linkset.json
quickbeam cdn bake --config ./audius-build/domains.audius.json --domain audius \
  --collection audius --cdn-dir ./audius-build/cdn
quickbeam cdn edges --cdn-dir ./audius-build/cdn --domain audius \
  --source ./audius-build/linkset.json
quickbeam cdn serve --cdn-dir ./audius-build/cdn --cors
```

## Why there's a cache

Fangorn is content-addressed: identical content must re-derive an identical CID, or
an unchanged commit re-uploads its whole graph. Audius is live — play counts tick,
trending reorders every few minutes. So the crawl writes a raw record cache and
`build_graph` reads only from that. Same cache → same CIDs → same commit.

It also means a flaky discovery node can't break the demo once the cache exists.
Re-crawling (`--refresh`) is then a deliberate act that produces a real, reviewable
diff — which is itself worth showing.

## Gotchas worth not re-learning

- **A real `User-Agent` is mandatory.** Discovery nodes sit behind Cloudflare, which
  403s the default `Python-urllib/3.x` while letting curl straight through.
- **`api.audius.co` advertises only itself.** The registry no longer returns a node
  list, and that host is a rate-limited load balancer — hence the low worker default
  and the adaptive 429 back-off (the client raises its own delay rather than
  retrying into the same wall).
- **`favorites` returns bare numeric ids**, not embedded objects like `reposts` does.
  They're resolved after the crawl, once every track is known; favorites pointing
  outside the crawled set are dropped rather than fetched.
- **`playlists_containing_track` is numeric too**, and every API path takes the
  *encoded* id — hence `hashids` (salt `azowernasdfoia`, min length 5, verified
  against the live pair `1192383172` ⇄ `AlQM0Bg`).
- **…and that field is both lossy and stale.** Roughly **two thirds of the playlists
  it names are deleted** (404 on the v1 *and* full APIs — they're gone, not private),
  and of the survivors many no longer actually contain the track. In one run, 80
  attempted yielded 4 usable platform→artist `contains` edges. Hence the high
  `--focus-playlists` default: it is a cap on *attempts*, not on edges.
- **Two id spaces.** Edge endpoints must match how the records were indexed:
  the offline `prebake` path keys on the staged node **name**, the on-chain `watch`
  path keys on the IPLD **vertex CID**. `audius-link --from OWNER:NS` (twice)
  rewrites the linkset into CID space, which is only possible *after* both pushes.
- **Genre/Mood/Tag converge for free.** Both sides derive them from their own
  tracks, the payloads are byte-identical, and content addressing gives both
  publishers the *same CID* — the graphs merge at those vertices with no linkset
  entry and no coordination.

## Tests

```sh
python -m pytest audius-source/tests/ -q
```

The one that matters is `test_partition_plus_linkset_loses_no_edge`: A + B + the
linkset must account for every edge the undivided graph has. Splitting a graph
across two sovereign publishers has to destroy nothing.
