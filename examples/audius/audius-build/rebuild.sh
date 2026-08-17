#!/usr/bin/env bash
set -e
# CWD is examples/audius/, so every ./audius-build/... path below is relative to
# it. $REPO is the separate anchor for repo-root things (venv, gpu-env.sh) that
# do not live alongside this example.
cd "$(dirname "$0")/.."
REPO=$(cd ../.. && pwd)
# GPU libraries. Sourced rather than hardcoded: the old inline version pointed at
# nvidia/cublas/lib, which has never existed for these wheels (cuBLAS ships under
# nvidia/cu13/lib), so it silently left embedding on the CPU at ~1/10th speed.
# Still a harmless no-op on a CPU-only install.
source "$REPO/gpu-env.sh"
rm -rf audius-build/stage
"$REPO/venv/bin/python" -m quickbeam.cli data audius --side A --cache-file ./audius-build/audius_cache.json \
  --output-dir ./audius-build/stage --volume 1
"$REPO/venv/bin/python" -m quickbeam.cli data audius --side B --cache-file ./audius-build/audius_cache.json \
  --output-dir ./audius-build/stage --volume 2
"$REPO/venv/bin/audius-link" --cache-file ./audius-build/audius_cache.json --out ./audius-build/linkset.json
curl -s -X DELETE http://localhost:6333/collections/audius > /dev/null
curl -s -X PUT http://localhost:6333/collections/audius -H 'Content-Type: application/json' \
  -d '{"vectors":{"size":256,"distance":"Cosine"}}' > /dev/null
"$REPO/venv/bin/python" -m quickbeam.cli data prebake --input-dir ./audius-build/stage --volume 1 \
  --collection audius --dim 256 --role-map-file ./audius-build/role_map.json \
  --owner 0x1111111111111111111111111111111111111111
"$REPO/venv/bin/python" -m quickbeam.cli data prebake --input-dir ./audius-build/stage --volume 2 \
  --collection audius --dim 256 --role-map-file ./audius-build/role_map.json \
  --owner 0x2222222222222222222222222222222222222222
rm -rf audius-build/cdn
# --shard-size 5000: Cloudflare Pages REJECTS any file over 25 MiB, and the default
# (50k points = every record in one file) produces a 63.7 MiB shard that cannot be
# deployed. At ~2.57 KiB/point this yields 6 shards of ~10.6 MiB. The client already
# loops over manifest.shards, so nothing downstream changes.
# Run the bake from $REPO: `bundle_schema` inside a domains config is resolved
# against the CWD, not the config's own location, so all three domains.*.json in
# this repo spell it repo-root-relative. Same --cdn-dir, just addressed from there.
( cd "$REPO" && venv/bin/python -m quickbeam.cli cdn bake \
    --config ./examples/audius/audius-build/domains.audius.json \
    --domain audius --collection audius \
    --cdn-dir ./examples/audius/audius-build/cdn --shard-size 5000 )
"$REPO/venv/bin/python" - <<'PY'
import json
e=[]
for p in ["audius-build/stage/volume_1_edges.json","audius-build/stage/volume_2_edges.json"]:
    e+=json.load(open(p))
e+=json.load(open("audius-build/linkset.json"))["edges"]
json.dump({"edges":e}, open("audius-build/edges_all.json","w")); print(len(e),"edges")
PY
"$REPO/venv/bin/python" -m quickbeam.cli cdn edges --cdn-dir ./audius-build/cdn --domain audius \
  --source ./audius-build/edges_all.json
echo "REBUILD DONE"
