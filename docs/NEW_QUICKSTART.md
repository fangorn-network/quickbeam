# Quickstart — the git-native flow

The successor to [`QUICKSTART.md`](./QUICKSTART.md). Same pipeline (Places + Events →
embeddings → CDN), but the publish step is now **git for data**: a dataset is a
**repo**, publishing is **commit + push**, and updating is a **new commit built on the
last one**. quickbeam then builds **only what changed** off the commit's parent diff.

> **Status legend**
> - ✅ **works today** — record-set repos end-to-end; `commit`/`push`/`log`/`show`/
>   `status`/`clone`; **`fangorn commit --bundle` and `commit --view`** (graphs and
>   views commit on the same rail as record-sets); the commit-diff `watch`/`build`
>   (embed-contract inheritance + delete propagation).
> - 🚧 **planned** — `quickbeam data publish` (a thin quickbeam wrapper over
>   `fangorn commit`), and porting the **sharded** bundle path off `publish_bundle.ts`.
>   The scripts still exist as the sharded / one-shot escape hatch and now share their
>   data-shaping code with the CLI (`src/cli/bundle-source.ts`), so the two can't drift.

Conventions: `fangorn …` runs in the **fangorn** repo (`~/fangorn/fangorn`);
`quickbeam …` runs in the **quickbeam** repo (`~/fangorn/embeddings`).

---

## The model in one screen

| git | Fangorn | command |
|---|---|---|
| `git init` | create a local dataset repo | `fangorn repo init <name> -s <schema>` |
| working tree | your staged JSON records / nodes+edges | files on disk |
| `git commit` | build a tree, pin blobs to IPFS, move **local** HEAD (permissionless) | `fangorn commit … -m <msg>` |
| `git push` | move the **on-chain** tip to your commit (the one permissioned step) | `fangorn push` |
| `git log` / `git show` | walk history / diff a commit vs. its parent, from IPFS | `fangorn log` / `fangorn show` |
| `git clone` | rebuild the repo from the on-chain tip + IPFS, no subgraph | `fangorn clone <owner> -s -d` |

Two properties fall out of this and drive everything downstream:

- **Structural sharing** — blobs are content-addressed, so a commit that changes k of n
  pages re-uploads k, not n. `fangorn commit` prints `Chunks: N (X uploaded, Y reused)`.
- **Deletes propagate** — dropping a record in the next commit yields a tree that omits
  its blob. quickbeam diffs the new tip against the last-built commit and **tombstones**
  exactly the removed entities from the index (no more orphaned points).

---

## 0. Setup (once)

Same environment as the classic quickstart — see
[`QUICKSTART.md` §0](./QUICKSTART.md#0-setup-once) for the Qdrant/Postgres containers and
the scrape/shape stages. The only credentials the git-native CLI needs:

```bash
# fangorn CLI reads these, or ~/.fangorn/config.json (`fangorn init`)
export DELEGATOR_ETH_PRIVATE_KEY=0x... PINATA_JWT=... PINATA_GATEWAY=... CHAIN_NAME=base-sepolia

# quickbeam build/watch read the subgraph + an IPFS gateway (reused as $BUILD_AUTH)
export GRAPH_API_KEY=... IPFS_GATEWAY=https://<your-gateway>/ipfs IPFS_GATEWAY_KEY=...
BUILD_AUTH="--graph-api-key $GRAPH_API_KEY --ipfs-gateway $IPFS_GATEWAY --ipfs-gateway-key $IPFS_GATEWAY_KEY"
export STAGE=~/fangorn/embeddings/stage_volumes
```

---

## A. A record-set repo, the pure git way ✅ (repo mechanics work today)

The clearest demonstration of the new **repo mechanics**. A dataset with no graph — just
records — is a repo you `commit` and `push`, then update with a second commit that **adds
and removes** records, with parented history and structural sharing. (Embedding a
record-set repo through the commit-diff watcher is part of the planned wiring — see the
note at the end of this section; the commit-diff *embedding* runs on the bundle/view path
in §B.)

```bash
cd ~/fangorn/fangorn

# 1. a schema for the records (once) — prompts for chain + the JSON schema file path
fangorn schema register my.notes.v1                   # or reuse an existing schema name

# 2. init the repo — dataset name + the schema it conforms to
fangorn repo init places-notes -s my.notes.v1

# 3. commit v1 (100 records) → builds tree, pins blobs, moves LOCAL head. No chain write.
fangorn commit ./notes.v1.json -m "initial 100 notes"
#   Commit:  bafy…   Chunks: 100 (100 uploaded, 0 reused)

# 4. push — the single permissioned step: on-chain tip → your commit CID
fangorn push
#   Tx: 0x…   Tip: bafy…

fangorn status         # local HEAD vs on-chain tip
fangorn log            # walk the parent chain from IPFS
```

Now update the same repo — drop 10 stale notes, add 5 new ones — as a **second commit**:

```bash
# notes.v2.json has the 90 kept + 5 new (the 10 dropped are simply absent)
fangorn commit ./notes.v2.json -m "drop 10 stale, add 5"
#   Commit:  bafy…   Parent: bafy…(v1)   Chunks: 95 (5 uploaded, 90 reused)   ← structural sharing
fangorn show           # +blobs / -blobs vs the parent
fangorn push
```

Inspect and clone — all from IPFS, no subgraph:

```bash
fangorn show                                          # +blobs / -blobs vs the parent
fangorn clone 0x<owner> -s my.notes.v1 -d places-notes
#   rebuilds the whole repo — every commit — walking parents in IPFS
```

> **Embedding a record-set repo (🚧 planned).** `quickbeam watch`/`build` today target the
> **bundle/view** join paths (that's where the commit-diff — embed-contract inheritance +
> delete propagation — is wired). The flat record-set build path is not yet commit-aware,
> so the embed side of this section is the planned half. The **repo mechanics above are
> real today**; the commit-diff *embedding* (embed-contract inheritance + delete
> propagation) runs against real commit tips on the bundle/view path in §B, which now
> commits through `fangorn commit --bundle/--view`.

---

## B. The Places bundle + View, git-native ✅

The Places pipeline is a **graph** (typed nodes + edges) fused across sources by a
**View**. In the git-native model a bundle and a view are just other kinds of tree, so
publishing them is the same `commit` + `push` you used for records in §A — now via
`fangorn commit --bundle` and `--view`.

Scrape + shape are unchanged from the classic quickstart
([§A steps 1–3](./QUICKSTART.md#a-one-shot-demo--single-merged-bundle)):

```bash
cd ~/fangorn/embeddings
quickbeam data placespg --output-dir $STAGE      # places → volume_1_*
quickbeam data eventspg --output-dir $STAGE      # events → volume_2_*
quickbeam data schemagen --input-dir $STAGE --volume 0 \
  --prefix eagleriver.sond3r.com --bundle-name localcore --version v1
```

### Publish the bundle as a commit ✅

`commit --bundle` reads the schemagen stage dir, registers the node + bundle schemas
(idempotent), streams every node and edge into one tree, and wraps it in a commit on your
local HEAD. `repo init` first (the bundle schema id is deterministic, so this works before
the schema is registered — `commit` registers it). Optional `--embed-*` flags stamp the
Gap-A contract quickbeam inherits.

```bash
cd ~/fangorn/fangorn
fangorn repo init localcore -s eagleriver.sond3r.com.localcore.v1
fangorn commit --bundle $STAGE --volume 0 -m "eagle river places+events v1" \
  --embed-model nomic-ai/nomic-embed-text-v1.5 --embed-dim 768
#   Committed bundle (local)  ·  Nodes: …  Edges: …  ·  Chunks: N (X uploaded, Y reused)
fangorn push
#   Tx: 0x…   Tip: bafy…   ← the on-chain tip now points at the bundle commit

# Escape hatch — a HUGE bundle that needs laptop-sized (sharded) transactions still
# uses the script (shares its shaping code with the CLI; no commit history yet):
#   pnpm dotenvx run -f .env -- tsx src/test/publish_bundle.ts \
#     --input-dir $STAGE --volume 0 --shard-roots 50000 --root-type Business
```

### Fuse sources with a View ✅

A view commits the same way: `commit --view` registers the view (idempotent by name),
resolves its sources, and wraps the fuse recipe in a commit. (A view commit's parents are
your local HEAD today; making them the exact source *tips* it fused is slice S4.)

```bash
fangorn repo init localview -s eagleriver.sond3r.com.localview.v1
fangorn commit --view eagleriver.sond3r.com.localview.v1 \
  --source-bundle eagleriver.sond3r.com.osm.placecore.v1 \
  --source-bundle eagleriver.sond3r.com.evt.eventcore.v1:tribe -m "local view v1"
fangorn push
#   Committed view (local) → pushed. View id is the repo's schema id.

# Equivalent one-shot script (same source-resolution code as the CLI):
#   pnpm dotenvx run -f .env -- tsx src/test/publish_view.ts \
#     --name eagleriver.sond3r.com.localview.v1 \
#     --source-bundle eagleriver.sond3r.com.osm.placecore.v1 \
#     --source-bundle eagleriver.sond3r.com.evt.eventcore.v1:tribe
```

### Build + bake (unchanged)

```bash
cd ~/fangorn/embeddings
quickbeam build --view "eagleriver.sond3r.com.localview.v1=0x<viewId>" $BUILD_AUTH \
  --profiles-file ~/fangorn/embeddings/osm_profiles.json \
  --root-profile business --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
```

`build`/`watch` already handle git-native tips: if the on-chain CID is a commit they
follow it to its tree and inherit its embed contract; if it's a legacy raw manifest they
use it directly. So the build step above works whether the bundle/view was published by
`fangorn commit` or by the script escape hatch.

---

## C. Update a source = a new commit ✅

When *Tribe itself* posts new events, you re-commit **only the Tribe repo**. Same repo,
new parented commit → new on-chain tip at the same resourceId. Nothing else is touched,
and the View auto-resolves the newest tip.

```bash
quickbeam data events-fetch --source tribe --site https://eagleriver.org \
  --no-db --raw-out tribe_events.jsonl                 # idempotent upsert by event_key
quickbeam data eventspg --raw-in tribe_events.jsonl --volume 4 --output-dir $STAGE

# from the Tribe repo dir — a second parented commit on the same dataset:
cd ~/fangorn/tribe-events && fangorn commit --bundle $STAGE --volume 4 -m "tribe: +new events"  &&  fangorn push
#   Escape hatch: … tsx src/test/publish_bundle.ts --input-dir $STAGE --volume 4 --dataset tribe

# rebuild the view (picks up the new tip automatically)
quickbeam build --view "eagleriver.sond3r.com.localview.v1=0x<viewId>" $BUILD_AUTH \
  --profiles-file ~/fangorn/embeddings/osm_profiles.json \
  --root-profile business --root-profile localevent --reset
quickbeam cdn bake --collection fangorn --domain places --cdn-dir ./cdn
```

Because it's now a *parented commit* (not a blind overwrite), you get history for free:
`fangorn log` shows every version of the Tribe dataset, and a running `quickbeam watch`
tombstones any events Tribe dropped between versions.

---

## Where the publish scripts are going

`publish_bundle.ts` / `publish_view.ts` do two separable jobs. The git-native cut splits
them along that seam — most of it **is now done** (slice S1b):

- **Publish mechanics → the CLI (done).** Building the tree, pinning blobs, wrapping a
  commit, and the on-chain tip move are now `fangorn commit --bundle/--view` + `fangorn
  push` — the *same* primitives record-sets use. Bundles and views gain parented history,
  structural sharing, and the inherited embed contract, which the
  raw-`publishBundle`/`publishView` path did **not** provide.
- **Data shaping → shared, heading to quickbeam (in progress).** Streaming `stage_volumes`,
  conforming fields, type→file resolution, and idempotent schema registration are now
  factored into `fangorn/src/cli/bundle-source.ts`, imported by **both** the CLI and the
  scripts so they can't drift. The next step exposes this as `quickbeam data publish`
  (a thin wrapper that shells out to `fangorn commit`).
- **Sharding (still in the script).** The sort-merge, laptop-sized-transaction path
  (`--shard-roots`) still lives in `publish_bundle.ts` as the escape hatch for huge graphs;
  porting it behind `quickbeam data publish` is the follow-on that fully retires the script.

Net: the SDK already owns one publish path (commit/push) for every tree kind; the scripts
are shrinking to a thin wrapper + the sharded escape hatch. See
`fangorn/docs/GIT_NATIVE_IMPLEMENTATION_PLAN.md` (slice **S1b** for this work; S4 views,
S5 linksets next) for the sequencing.
