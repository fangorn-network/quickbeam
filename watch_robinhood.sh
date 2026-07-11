#!/usr/bin/env bash
# Robinhood watcher — subscribes to the owner's `robinhood` Fangorn namespace
# (`fangorn subscribe`, push-based light client) and embeds the Asset/Transfer
# graph into Qdrant + CDN as commits land. Run from repo root: ./watch_robinhood.sh
set -euo pipefail
cd "$(dirname "$0")"

# The wallet address `quickbeam data robinhood --publish` published under (see
# `fangorn head <address>` / `fangorn repo init robinhood` — same wallet the
# `fangorn` CLI is configured for via ~/.fangorn/config.json or its env vars).
: "${ROBINHOOD_OWNER:?export ROBINHOOD_OWNER=0x... (the publisher wallet address) before running this script}"

# How to invoke the fangorn CLI `quickbeam watch` shells out to for `read`/`subscribe`.
# Defaults to a global install; override for the dev build, e.g.:
#   export FANGORN_BIN="dotenvx run -f ~/fangorn/fangorn/.env -- node ~/fangorn/fangorn/lib/cli/cli.js"
FANGORN_BIN="${FANGORN_BIN:-fangorn}"

# Dedicated checkpoint + role-map files: the defaults (./db/ingest_checkpoint.json,
# ./db/role_map.json) are shared across every watched source — a robinhood run must
# never skip/clobber another domain's state (and vice versa). If you wipe the
# `robinhood` Qdrant collection, delete the checkpoint file too, or the watcher
# will consider every vertex already processed and never re-embed.
# Prefer the repo venv so the script works without an activated shell.
QUICKBEAM="quickbeam"
[[ -x ./venv/bin/quickbeam ]] && QUICKBEAM=./venv/bin/quickbeam

exec "$QUICKBEAM" watch \
  --source "${ROBINHOOD_OWNER}:robinhood" \
  --fangorn-bin "$FANGORN_BIN" \
  --root-profile asset \
  --root-profile transfer \
  --collection robinhood \
  --checkpoint-file ./db/robinhood_checkpoint.json \
  --role-map-file ./db/robinhood_role_map.json \
  --cdn-dir ./cdn \
  --cdn-domain robinhood \
  --poll-interval 30   # reconnect backoff if the subscribe stream drops (not a poll timer)
