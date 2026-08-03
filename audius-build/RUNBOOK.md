# Audius demo — runbook

Two sovereign graphs, two wallets, one linkset.

## What's built (verified locally 2026-08-03)

| | |
|---|---|
| Crawl cache | 46,260 records (104 MB) — the determinism boundary |
| Graph A — platform | 25,306 nodes / 107,454 edges, Disclosure's catalog removed |
| Graph B — Disclosure | 99 nodes / 300 edges |
| Linkset | 113 cross-graph edges: 55 `contains`, 52 `remixOf`, 4 `favorited`, 1 `reposted`, 1 `sameAs` |
| Qdrant `audius` | 25,372 points @256d — note 25,405 staged, **33 shared vocabulary nodes converged** |
| Served CDN | 65.7 MB single shard; 107,867 edges across **16 relation types**; **0 unresolved endpoints** |
| Cross-wallet | **113/113** linkset edges join records published by different wallets |

Types: Track 11,112 · Tag 8,925 · Artist 4,399 · Playlist 789 · Genre 124 · Mood 23.

**Snapshot size.** 65.7 MB is fine served locally (how you'd demo in the room) but slow
over conference wifi. Tag is 8,925 of 25,372 points and every point costs about the
same (the 256-float vector dominates), so excluding Tag as a *node type* would cut it
to roughly 42 MB — at the price of the `taggedWith` relation. Tags stay searchable
either way; they're already in each track's embedded text and `tags` field. Everything here runs from
`/home/coleman/fangorn/quickbeam` (the editable `quickbeam` install is stale, so only
the repo root resolves the import).

```sh
cd /home/coleman/fangorn/quickbeam
export LD_LIBRARY_PATH=$PWD/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:\
$PWD/venv/lib/python3.12/site-packages/nvidia/cublas/lib:$LD_LIBRARY_PATH
docker start qdrant
```

`MB=./audius-build` throughout.

---

## Part 1 — local (already done; re-run to rebuild)

```sh
# 1. Crawl once. Everything after this is offline.
venv/bin/python -m quickbeam.cli data audius --dry-run --side all \
  --cache-file $MB/audius_cache.json \
  --max-artists 150 --max-trending 200 --max-playlists 60 \
  --focus-playlists 250 --per-artist-tracks 30 --workers 4

# 2. Stage the two graphs from that one cache (seconds, no network).
venv/bin/python -m quickbeam.cli data audius --side A \
  --cache-file $MB/audius_cache.json --output-dir $MB/stage --volume 1
venv/bin/python -m quickbeam.cli data audius --side B \
  --cache-file $MB/audius_cache.json --output-dir $MB/stage --volume 2

# 3. Embed both into ONE collection, badged by publisher.
curl -X PUT http://localhost:6333/collections/audius \
  -H 'Content-Type: application/json' -d '{"vectors":{"size":256,"distance":"Cosine"}}'
venv/bin/python -m quickbeam.cli data prebake --input-dir $MB/stage --volume 1 \
  --collection audius --dim 256 --role-map-file $MB/role_map.json --owner $WALLET_A
venv/bin/python -m quickbeam.cli data prebake --input-dir $MB/stage --volume 2 \
  --collection audius --dim 256 --role-map-file $MB/role_map.json --owner $WALLET_B

# 4. Linkset + bake + install the full relational axis.
venv/bin/audius-link --cache-file $MB/audius_cache.json --out $MB/linkset.json
venv/bin/python -m quickbeam.cli cdn bake --config $MB/domains.audius.json \
  --domain audius --collection audius --cdn-dir $MB/cdn
#    cdn edges OVERWRITES, so ship intra-graph edges + linkset in ONE file:
venv/bin/python -c "
import json
e=[]
for p in ['$MB/stage/volume_1_edges.json','$MB/stage/volume_2_edges.json']: e+=json.load(open(p))
e+=json.load(open('$MB/linkset.json'))['edges']
json.dump({'edges':e}, open('$MB/edges_all.json','w')); print(len(e),'edges')"
venv/bin/python -m quickbeam.cli cdn edges --cdn-dir $MB/cdn --domain audius \
  --source $MB/edges_all.json

# 5. Serve + browse.
venv/bin/python -m quickbeam.cli cdn serve --cdn-dir $MB/cdn --cors --port 8090
cd examples && VITE_DATA_SOURCE=shards VITE_CDN_URL=http://localhost:8090 \
  VITE_DOMAIN=audius VITE_CLAIMS=off npm run dev
```

**Ordering traps:** `cdn bake` rmtrees the domain directory, so `cdn edges` must run
**after** it. `bake` reads `dim` from the source collection, not the config — to
change dim you re-embed.

If the browser snapshot is too heavy for a live demo, `cdn bake --limit N` trims it;
the linkset then loses any edge whose endpoint got cut (`audius-link` will report
them as unresolved, which is the signal to raise the limit).

---

## Part 2 — on-chain (you run this; two wallets)

The demo only *proves* sovereignty if the two graphs settle to **two different
on-chain roots**. One wallet with two namespaces shares a single root per publisher
and a CTO will notice.

```sh
# Each wallet needs gas + registration. Registration is required even for the
# storage worker's free tier — it 403s unregistered callers before the free bytes
# ever apply.
#   → fund both from the faucet, then, with each key in ~/.fangorn/config.json:
fangorn register
```

`PINATA_JWT` must come from `fangorn/.env`. `~/.fangorn/config.json` ships
`pinataJwt: ""`, which silently routes uploads to the shared worker account — whose
pins get swept without notice, orphaning the root.

```sh
# Publish the platform graph as wallet A:
venv/bin/python -m quickbeam.cli data audius --side A \
  --cache-file $MB/audius_cache.json --output-dir $MB/stage --volume 1 \
  --publish --namespace audius

# Publish the artist graph as wallet B (swap the key in ~/.fangorn/config.json first):
venv/bin/python -m quickbeam.cli data audius --side B \
  --cache-file $MB/audius_cache.json --output-dir $MB/stage --volume 2 \
  --publish --namespace disclosure
```

Then rebuild the linkset in **CID space** — endpoints must be vertex CIDs once the
records come from the chain, and CIDs don't exist until after the push:

```sh
venv/bin/audius-link --cache-file $MB/audius_cache.json \
  --from $WALLET_A:audius --from $WALLET_B:disclosure \
  --out $MB/linkset.cid.json
```

It exits non-zero and lists offenders if any endpoint fails to resolve. That count
must be **0** — an unresolved endpoint is a dead end in the served demo.

Finally, read both roots back and re-bake:

```sh
venv/bin/python -m quickbeam.cli watch \
  --source $WALLET_A:audius --source $WALLET_B:disclosure \
  --collection audius --dim 256
# then: cdn bake → cdn edges (with linkset.cid.json) → cdn serve
```

`watch` ingests the two sources **independently** — there is no cross-source identity
fusion in the ingest path (the README's union-find `--view` no longer exists). The
fusion is the linkset, applied at the CDN layer. That is the design, not a shortfall:
the join is data anyone can inspect, not a build step either publisher controls.

---

## What to show, in order

1. **Search** — "dark acid groove house with a moody bassline". Results interleave
   both publishers; the pill on each card is the wallet that published it.
2. **Search the artist's name** — the platform's thin stub and the artist's own rich
   node come back side by side, from two different wallets.
3. **Traverse `sameAs`** — the two identities are asserted equal by a linkset entry,
   not by either party's database.
4. **Traverse `contains`** — a playlist in one graph whose tracks live in the other.
5. **The vocabulary** — both publishers independently derived `Genre: Electronic`.
   Identical payload bytes ⇒ identical CID, so the two graphs *merge* at that vertex
   with no linkset entry and no coordination. Content addressing doing the work a
   schema registry usually does.
6. **The point**: Audius's discovery today needs every artist's data inside Audius's
   index. Here the platform's graph has a hole shaped like the artist, the artist
   fills it from their own wallet, and search spans both anyway.
