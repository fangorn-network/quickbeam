# audius-demo

The browser front end for the Audius pitch: **two independent publishers, one
searchable music graph**, styled to Audius and running entirely client-side.

## Run it

```sh
# 1. serve the baked snapshot (from quickbeam/)
#    no cdn/ dir yet? see "Build the snapshot from scratch" below
venv/bin/python -m quickbeam.cli cdn serve --cdn-dir ./examples/audius/audius-build/cdn --cors --port 8090

# 2. the app
cd examples/audius/audius-demo && npm install && npm run dev      # http://localhost:5180
```

### The other client

This app is not the only consumer of that snapshot. `quickbeam mcp` pulls the same shards and the same linkset and hands them to an **agent** as tools which give semantic search plus typed traversal, with the same "the data comes to you, the query stays here" property this app has. `.mcp.json` at the repo root registers it which reads from the `cdn serve` command from step 1. Setup and the traversal recipe are in [`audius-source/README.md`](../audius-source/README.md#let-an-agent-explore-it-mcp).

### The third client — an agent inside this tab

`quickbeam mcp` is a pull-client: the snapshot comes to the process, so the queries
never leave it. **WebMCP is that argument with no process at all.** The tab has already
downloaded the graph, so this page registers its own verbs on `document.modelContext`
([WebMCP](https://github.com/webmachinelearning/webmcp)) and a browser-resident agent
calls them directly. No server, no transport, **no dependency** — see
[`src/lib/webmcp.ts`](src/lib/webmcp.ts).

Fourteen tools: `search-music`, `open-record`, `describe-graph`, `list-relations`,
`traverse`, `browse`, `player-state`, `control-player`, `read-taste`, `recommend`,
`list-playlists`, `create-playlist`, `add-to-playlist`, `share-playlist`.

```sh
scripts/webmcp-chrome.sh          # Chrome, flag on, throwaway profile
npm run check:webmcp              # the tools' self-check — no browser, no network
```

In the tab's console (Chrome wants the tool **object**, not its name, and takes the
arguments as a JSON **string**):

```js
const mc = document.modelContext;
const t = (await mc.getTools()).find((x) => x.name === "search-music");
await mc.executeTool(t, JSON.stringify({ query: "late night driving synthwave", limit: 3 }));
```

Four things this is built around, rather than assumed:

- **`document.modelContext` is undefined for almost everyone.** It is behind a Chrome
  flag. The hook feature-detects and returns, so the app is byte-identical without it —
  that path is the common one and is the one to check after any change here.
- **The tools read the world through a ref, and register exactly once.** An effect with
  the deps object in its dependency array re-registers on every render, and the agent
  watches fourteen tools vanish and reappear continuously while calls in flight die on
  the abort. It is also why mounting is not gated on the snapshot having loaded: a tool
  registered now sees the graph when it arrives.
- **Search results carry `duration` and `mood`.** That is not payload trimming left
  generous, it is the feature: without them an agent can retrieve but not *compose*, and
  *"make a playlist called 'party tonight', high energy tapering off, one hour long"*
  stops being answerable. `check-webmcp.ts` drives exactly that request end to end.
- **Only two tools write, and they differ on purpose.** `create-playlist` saves without
  interrupting — you asked for a playlist and nothing you had is touched.
  `add-to-playlist` changes something you already made, so it raises a card and waits;
  dismissal and silence both mean no. Nothing renames or deletes.

### Showing it on another device (ngrok, Cloudflare tunnel, a phone)

Tunnel **only the app port**. The snapshot rides along with it:

```sh
ngrok http 5180
```

The dev server proxies `/cdn` → `http://localhost:8090`, so the browser fetches the snapshot from the same origin it loaded the page from.

Do **not** set `VITE_CDN_URL` to `http://localhost:8090` for this. That address means "this device".

If `cdn serve` runs somewhere other than `localhost:8090`, point the *proxy* at it, `CDN_TARGET=http://host:port npm run dev`, not the browser.

| Env var | Default | Purpose |
|---|---|---|
| `VITE_CDN_URL` | `/cdn` (same origin, proxied) | Override only if the CDN is served elsewhere over https |
| `CDN_TARGET` | `http://localhost:8090` | Where the dev server's `/cdn` proxy forwards to |
| `VITE_DOMAIN` | `audius` | Baked domain name |
| `VITE_CONTENT_NODE` | `https://creatornode.audius.co` | Resolves artwork CIDs |
| `VITE_DISCOVERY_NODE` | `https://discoveryprovider.audius.co` | Resolves `/stream` for playback |
| `VITE_PLATFORM_OWNER` | `0x1111…1111` | Which wallet is the platform with the other being the artist |

First load pulls the whole snapshot once. The first search additionally downloads the embedding model from the HuggingFace CDN. After that, searching is offline.

## Build the snapshot from scratch

Step 1 above serves `../audius-build/cdn`, which does **not** exist in a fresh clone since it is a gitignored build artifact. The full guide, including the on-chain Part 2, is in [`../audius-build/RUNBOOK.md`](../audius-build/RUNBOOK.md). Everything below runs **from the repo root** (`quickbeam/`).

### Once per machine

```sh
python3.12 -m venv venv
venv/bin/pip install -e ".[cpu]"                        # .[gpu] optional - see below
venv/bin/pip install -e examples/audius/audius-source   # registers `quickbeam data audius` + `audius-link`

docker run -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant
```

Publish **both** ports. Qdrant answers REST on 6333 and gRPC on 6334, and the build uses both: `rebuild.sh`'s `curl` calls create the collection over REST, while `data prebake` and `cdn bake` build their client with `prefer_grpc=True` and talk to 6334.

If 6333 is already taken, or your Qdrant wants an API key, you can override it:
```sh
QDRANT_PORT=7333 QDRANT_GRPC_PORT=7334 bash examples/audius/audius-build/rebuild.sh
# QDRANT_API_KEY=… is threaded through to curl, prebake and bake when set
```

### Crawl for Data

```sh
venv/bin/python -m quickbeam.cli data audius --dry-run --side all \
  --cache-file ./examples/audius/audius-build/audius_cache.json \
  --max-artists 150 --max-trending 200 --max-playlists 60 \
  --focus-playlists 250 --per-artist-tracks 30 --workers 4
```

`--dry-run` is not "do nothing" here. The crawl cache is written either way, and the flag only suppresses the staged volume files which `rebuild.sh` writes itself in the next step. `--side all` fills the cache for both publishers in one pass.

This is the **only** step that touches the network. It reads Audius' live API and trending moves daily, so a re-crawl gives the same shape but different records so the focus artist's catalog may differ. Re-running it later is a no-op unless you pass `--refresh`.

### Build the Data

```sh
bash examples/audius/audius-build/rebuild.sh      # prints REBUILD DONE
```

Running rebuild.sh is deterministic. It drops the `audius` collection and rmtrees `stage/` and `cdn/` on every run then it does:

1. **Stage** two graphs from the one cache (seconds, no network). Side A is the platform, side B the artist, with the artist's own catalog removed from A so the two publishers hold different records.
2. **Linkset** `audius-link` derives the cross-graph edges that fuse them.
3. **Recreate** the `audius` collection in Qdrant, 256-dim, cosine.
4. **Embed** both volumes into that *one* collection, each badged with its publisher wallet (`0x1111…` / `0x2222…`). This can be slow with ~25k points. It takes about 15 minutes on a CPU.
5. **Bake** the CDN tree with `--shard-size 5000`.
6. **Edges** intra-graph edges and the linkset written as a single file because `cdn edges` overwrites rather than appends.

Two important flags:

- **`--dim 256`** has to equal `D` in `src/kernel/constants.ts` and the slice width in `src/lib/matryoshka.ts`. Documents are embedded with `nomic-ai/nomic-embed-text-v1.5` (natively 768) and matryoshka-sliced to 256. The browser repeats that transform on the query side. A difference between them degrades ranking which is what `npm run check` catches.
> Note `cdn bake` reads dim from the *collection* so changing it means re-embedding.
- **`--shard-size 5000`** see [File size constraints](#file-size-constraints).

### GPU (optional)

`.[gpu]` on its own is not enough. `fastembed-gpu` depends on the bare `onnxruntime-gpu`, which declares its CUDA and cuDNN wheels as *optional* extras, so nothing CUDA gets installed. The provider still registers and then dies partway through the graph:

```
NOT_IMPLEMENTED : ... Einsum node. Name:'/encoder/layers.0/attn/rotary_emb/Einsum'
cuDNN is unavailable or disabled for CUDA Execution Provider:
dlopen failed for libcudnn.so: cannot open shared object file
```

This is because it is missing a library. The `gpu` extra spells out `onnxruntime-gpu[cuda,cudnn]`, and `gpu-env.sh` puts `nvidia/*/lib` on `LD_LIBRARY_PATH` and creates the unversioned `libcudnn.so` alias that onnxruntime dlopen's but the wheel does not ship (only `libcudnn.so.9`). `rebuild.sh` sources it already; it re-creates the alias each time, because `pip install` drops it.

```sh
venv/bin/pip uninstall -y onnxruntime onnxruntime-gpu fastembed fastembed-gpu
venv/bin/pip install -e ".[gpu]"
source gpu-env.sh
venv/bin/python -c "from fastembed import TextEmbedding as T; \
  print(T(model_name='nomic-ai/nomic-embed-text-v1.5').model.model.get_providers())"
```

The uninstall is not optional. If you ever ran `.[cpu]` then `fastembed`/`fastembed-gpu` and `onnxruntime`/`onnxruntime-gpu` will share a package directory since installing one does not remove the other.

### Verify build

```sh
cd examples/audius/audius-demo
npm run check         # needs Qdrant up — asserts the query-side transform still
                      # matches the one used at ingest
npm run check:graph   # needs `cdn serve` on 8090 — 12 assertions over the served tree
npm run check:kernel  # 16 assertions over the recommender seam
npm run check:webmcp  # 25 assertions over the agent tools — no browser needed
```

`check:webmcp` needs nothing running at all — `src/lib/webmcp.ts` has zero imports, so
the 25 assertions drive the tools with a fake `modelContext` and stub dependencies.

`check:graph` catches a bad bake. It asserts both publishers are present, that every edge endpoint resolves, and that the baked `manifest.stats` equals what the graph contains.

## Deploy (Cloudflare Pages)

The snapshot is baked locally against Qdrant, so CI can't regenerate it. Build a self-contained `dist/` and directly upload it.

```sh
npm install
npm run build:static                       # stages ../audius-build/cdn → public/cdn, then builds
npx wrangler@latest login                  # once
npx wrangler@latest pages deploy dist --project-name audius-demo
```

Then attach the custom domain in the Cloudflare dashboard (Workers & Pages → the project → **Custom domains**, *not* a hand-made DNS CNAME since that returns a 522). `fangorn.network` is a zone on the same account, so Pages issues the CNAME and cert itself. On re-deploys, **purge the Cloudflare cache** so the new `index.html` is served.

### File size constraints

**Pages rejects any file over 25 MiB, and the default bake puts all 25,372 records in one 63.7 MiB shard.** That is why `examples/audius/audius-build/rebuild.sh` passes `--shard-size 5000`. It yields 6 shards of ~12.5 MiB.

If you re-bake by hand, keep that flag.

```
   20.60 MiB  ort-wasm-simd-threaded.jsep.wasm     ← the ONNX runtime, still under
   12.57 MiB  shard-0001…ndjson.gz   (×6)
   12.49 MiB  edges                                ← 107,867 edges
   ─────────
   18 files, 97.8 MiB total
```

### Verifying a build before you upload

`dist/` is a plain static tree, so serve it as one and point the checks at it. This exercises the multi-shard path and the extensionless URLs the same way Pages will:

```sh
(cd dist && python3 -m http.server 8099) &
VITE_CDN_URL=http://localhost:8099/cdn npm run check:graph    # 12 assertions
VITE_CDN_URL=http://localhost:8099/cdn npm run check:kernel   # 16 assertions
```

No env var is needed in production: `CDN_URL` defaults to the relative `/cdn`, so the app fetches the snapshot from whatever origin served the page.

## Playback

`fields.id` is the Audius track id, so the stream URL is `{discovery}/v1/tracks/{id}/stream?app_name=…`, which 302s to a content node serving `audio/mpeg` with byte ranges and open CORS which is everything a plain `<audio>` needs. Audius' redirect appends `skip_play_count=true`, so **this site does not inflate anyone's play counts**.

There is exactly **one** `<audio>` element for the whole app in `src/lib/player.tsx` and mounted above `App` so navigating between views doesn't stop the music. Playback is handled by the browser's media pipeline rather than JS.

`play(track, queue)` takes the list the click came from, so next/prev work inside a result grid, a relation rail, or a playlist without any queue UI. The now-playing bar carries the **publisher chip**, so whatever is playing states which wallet published it.

In the original bake, **1% of tracks are stream-gated or withdrawn** (measured: 101 of 11,112). Gated tracks report "unavailable"; `npm run check:stream` re-measures that against live data.

## The session kernel

`src/kernel/` is **sonder's** Markov kernel (`sonder/app/src/renderer/src/kernel/`), ported close to verbatim so the two stay diffable. It keeps a position μ and velocity v over the embedding space, plus taste accumulators, artist affinity, a skip-repulsion region, and entropy-coupled Gibbs sampling. Plays, skips and explicit ♥/– all feed into the user's kernel.

It runs **in the worker**.

## Privacy

**queries never leave the browser**. The searchable snapshot is downloaded once and searched locally, so no server sees what you typed or what you were looking for.

Two things do leave the browser:

- **Artwork** is fetched by CID from a content node as you browse.
- **Audio** streams from a content node when you press play.

Both are ordinary content-addressed retrieval of blobs the snapshot doesn't carry and both are visible to whichever node serves them.
