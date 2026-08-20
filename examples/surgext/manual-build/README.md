# manual-build — bake the surgext graph into a CDN snapshot

The data pipeline that produces the Semantic-CDN domain the [`../manual`](../manual) app reads.
Holds the bake spec + intermediate output:

- `domains.surgext.json` — the `quickbeam cdn bake` config: `role_map` (title / text / tags=category /
  relations), `presentation` (per-type accents + definitions), and a `bundle_schema` pointer.
- `surgext.bundle.json` — the type-level edge definitions (rel / from / to) baked into the manifest.
- `stage/` — the staged node/edge volumes (`volume_1_*.json`) from `quickbeam data surgext`.
- `images/` — the extracted figures (screenshots + rendered block diagrams, ~114 PNGs) and, once
  pinned, `image-cids.json` (see On-chain images below).
- `cdn/` — the baked output (`surgext/manifest.json`, `shard-*.ndjson.gz`, `edges.json`, `catalog.json`).

## Why 256 dims

The manual app embeds queries in-browser and truncates them to **256** (matryoshka), so the served
document vectors must be 256 too. The working Qdrant `surgext` collection is 768; the pipeline
re-embeds into a separate **256-dim** `surgext_cdn` collection just for baking.

## Regenerate

Run from the quickbeam root (so the `quickbeam` package is importable); pass **absolute** paths.

```bash
QB=/path/to/quickbeam            # the quickbeam project root
MB=$QB/examples/surgext/manual-build

# 1. Stage the graph (manual PDF + patch fusion) to node/edge volumes, and extract the
#    manual's figures (screenshots + block diagrams) into $MB/images (~114 files).
python -m quickbeam.cli data surgext --pdf-path $QB/../Surge-XT-Manual.pdf \
    --image-dir $MB/images --output-dir $MB/stage

# 2. Create a 256-dim collection and embed into it.
python - <<'PY'
from qdrant_client import QdrantClient, models
c = QdrantClient(host="localhost", port=6333)
c.recreate_collection("surgext_cdn",
    vectors_config=models.VectorParams(size=256, distance=models.Distance.COSINE))
PY
python -m quickbeam.cli data prebake --input-dir $MB/stage --volume 1 \
    --collection surgext_cdn --dim 256

# 3. Bake the domain, then install the edge linkset (bake AFTER edges is wrong —
#    `cdn bake` rmtrees the domain dir, so edges must run LAST).
python -m quickbeam.cli cdn bake  --config $MB/domains.surgext.json --domain surgext \
    --collection surgext_cdn --cdn-dir $MB/cdn
python -m quickbeam.cli cdn edges --domain surgext \
    --source $MB/stage/volume_1_edges.json --cdn-dir $MB/cdn

# 3b. Copy the figures into the baked domain (AFTER bake — it rmtrees the domain dir).
cp -r $MB/images $MB/cdn/surgext/images

# 4. Stage into the app: manifest + shards + edges, and PACK the figures into one privacy
#    bundle (images/ -> domains/surgext/images.json) so the app makes no per-figure request.
cd $QB/examples/surgext/manual && node scripts/stage-cdn.mjs --domain surgext
```

`bake` reads the vector `dim` from the source collection (not the spec), which is why step 2 must
create the collection at 256.

## On-chain images (optional, separate from the website)

The steps above are the off-chain website pipeline. To make the figures travel with a **published
on-chain** graph, pin them to IPFS and re-extract so their CIDs land in the payload — see the
"Figures & on-chain images" section of [`../surgext-source/README.md`](../surgext-source/README.md)
(`surgext-pin-images --image-dir $MB/images --pinata-jwt …`). The website does not need this; it
serves figures from the bundle above.
