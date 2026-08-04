#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
# ponytail: harmless no-op on a CPU-only install — the dirs just don't exist.
export LD_LIBRARY_PATH=$PWD/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:$PWD/venv/lib/python3.12/site-packages/nvidia/cublas/lib:$LD_LIBRARY_PATH
rm -rf audius-build/stage
venv/bin/python -m quickbeam.cli data audius --side A --cache-file ./audius-build/audius_cache.json \
  --output-dir ./audius-build/stage --volume 1
venv/bin/python -m quickbeam.cli data audius --side B --cache-file ./audius-build/audius_cache.json \
  --output-dir ./audius-build/stage --volume 2
venv/bin/audius-link --cache-file ./audius-build/audius_cache.json --out ./audius-build/linkset.json
curl -s -X DELETE http://localhost:6333/collections/audius > /dev/null
curl -s -X PUT http://localhost:6333/collections/audius -H 'Content-Type: application/json' \
  -d '{"vectors":{"size":256,"distance":"Cosine"}}' > /dev/null
venv/bin/python -m quickbeam.cli data prebake --input-dir ./audius-build/stage --volume 1 \
  --collection audius --dim 256 --role-map-file ./audius-build/role_map.json \
  --owner 0x1111111111111111111111111111111111111111
venv/bin/python -m quickbeam.cli data prebake --input-dir ./audius-build/stage --volume 2 \
  --collection audius --dim 256 --role-map-file ./audius-build/role_map.json \
  --owner 0x2222222222222222222222222222222222222222
rm -rf audius-build/cdn
# --shard-size 5000: Cloudflare Pages REJECTS any file over 25 MiB, and the default
# (50k points = every record in one file) produces a 63.7 MiB shard that cannot be
# deployed. At ~2.57 KiB/point this yields 6 shards of ~10.6 MiB. The client already
# loops over manifest.shards, so nothing downstream changes.
venv/bin/python -m quickbeam.cli cdn bake --config ./audius-build/domains.audius.json \
  --domain audius --collection audius --cdn-dir ./audius-build/cdn --shard-size 5000
venv/bin/python - <<'PY'
import json
e=[]
for p in ["audius-build/stage/volume_1_edges.json","audius-build/stage/volume_2_edges.json"]:
    e+=json.load(open(p))
e+=json.load(open("audius-build/linkset.json"))["edges"]
json.dump({"edges":e}, open("audius-build/edges_all.json","w")); print(len(e),"edges")
PY
venv/bin/python -m quickbeam.cli cdn edges --cdn-dir ./audius-build/cdn --domain audius \
  --source ./audius-build/edges_all.json
echo "REBUILD DONE"
