#!/usr/bin/env bash
# Robinhood bundle watcher — embeds the published Asset graph into Qdrant + CDN.
# Run from repo root:  ./watch_robinhood.sh
set -euo pipefail
cd "$(dirname "$0")"

# GRAPH_API_KEY / IPFS creds for the fangorn light-client read.
source quickbeam/.env

# Dedicated checkpoint + role-map files: the defaults (./db/ingest_checkpoint.json,
# ./db/role_map.json) are shared across every watched source — a robinhood run must
# never skip/clobber another domain's state (and vice versa). If you wipe the
# `robinhood` Qdrant collection, delete the checkpoint file too, or the watcher
# will consider every manifest already processed and never re-embed.
# Prefer the repo venv so the script works without an activated shell.
QUICKBEAM="quickbeam"
[[ -x ./venv/bin/quickbeam ]] && QUICKBEAM=./venv/bin/quickbeam

exec "$QUICKBEAM" watch \
  --source 0x147c24c5Ea2f1EE1ac42AD16820De23bBba45Ef6:robinhood \
  --collection robinhood \
  --checkpoint-file ./db/robinhood_checkpoint.json \
  --role-map-file ./db/robinhood_role_map.json \
  --cdn-dir ./cdn \
  --cdn-domain robinhood
