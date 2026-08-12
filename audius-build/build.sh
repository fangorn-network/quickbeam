#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
venv/bin/python -m quickbeam.cli data prebake --input-dir ./audius-build/stage --volume 1 \
  --collection audius --dim 256 --role-map-file ./audius-build/role_map.json \
  --owner 0x1111111111111111111111111111111111111111
venv/bin/python -m quickbeam.cli data prebake --input-dir ./audius-build/stage --volume 2 \
  --collection audius --dim 256 --role-map-file ./audius-build/role_map.json \
  --owner 0x2222222222222222222222222222222222222222
echo "PREBAKE DONE"
