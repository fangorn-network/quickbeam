#!/usr/bin/env bash
# Robinhood bundle watcher — embeds the published Asset graph into Qdrant + CDN.
# Run from repo root:  ./watch_robinhood.sh
set -euo pipefail
cd "$(dirname "$0")"

# Pull GRAPH_API_KEY / IPFS creds into BUILD_AUTH.
source quickbeam/.env

if [[ -z "${BUILD_AUTH:-}" ]]; then
  echo "BUILD_AUTH is empty after sourcing quickbeam/.env — aborting." >&2
  exit 1
fi

# Current market bundle (see stage_volumes/.fangorn/config.json — keep in sync
# after a `fangorn repo init` against a new schema version).
BUNDLE="test.robinhood.chain.market.07032026.1=0x5656c4bccb0d4c9cdd26bd60693687cb8b2e9bc30850a4cbb3a7d4c5b96643ca"

# Dedicated checkpoint + role-map files: the defaults (./db/ingest_checkpoint.json,
# ./db/role_map.json) are shared across every watched bundle — a robinhood run must
# never skip/clobber another domain's state (and vice versa). If you wipe the
# `robinhood` Qdrant collection, delete the checkpoint file too, or the watcher
# will consider every manifest already processed and never re-embed.
# Prefer the repo venv so the script works without an activated shell.
QUICKBEAM="quickbeam"
[[ -x ./venv/bin/quickbeam ]] && QUICKBEAM=./venv/bin/quickbeam

exec "$QUICKBEAM" watch \
  --bundle "$BUNDLE" \
  --root-profile asset \
  --root-profile transfer \
  --collection robinhood \
  --checkpoint-file ./db/robinhood_checkpoint.json \
  --role-map-file ./db/robinhood_role_map.json \
  --cdn-dir ./cdn \
  --cdn-domain robinhood \
  --poll-interval 60 \
  $BUILD_AUTH
