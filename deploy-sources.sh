#!/usr/bin/env bash
# Deploy quickbeam to the GCE box with a STATIC watch list: the JSON file in
# data/ instead of the registry worker's /watchlist.
#
#   ./deploy-sources.sh [--env] [--dry-run] [path/to/sources.json]
#
# The image half is deploy.sh's job and is not duplicated here; this only adds
# the watch list. The file goes INTO the shared `data` volume via
# `docker compose cp`, not onto the box's disk — /data in the container IS that
# volume, so a copy left in the home directory is invisible to `watch`.
set -euo pipefail
cd "$(dirname "$0")"

INSTANCE=${INSTANCE:-quickbeam-1}
ZONE=${ZONE:-us-east4-a}

SOURCES=data/sources.json
args=()
for a in "$@"; do
  case "$a" in
    --env|--dry-run) args+=("$a") ;;
    -*) echo "usage: $0 [--env] [--dry-run] [sources.json]" >&2; exit 2 ;;
    *) SOURCES=$a ;;
  esac
done
DRY=0; [[ " ${args[*]} " == *" --dry-run "* ]] && DRY=1
run() { if [[ $DRY -eq 1 ]]; then echo "  + $*"; else "$@"; fi; }

[[ -f $SOURCES ]] || { echo "no watch list at $SOURCES" >&2; exit 1; }
# Validate before shipping: nothing downstream treats a bad list as an error.
# _fetch_sources silently skips an entry it cannot parse, and DROPS one that
# names no app — so a typo here comes back as a box that watches nothing and
# says so exactly once, in a log line nobody reads.
python3 - "$SOURCES" <<'PY'
import json, sys
items = json.load(open(sys.argv[1]))
items = items.get("sources", []) if isinstance(items, dict) else items
if not items:
    sys.exit("watch list is empty")
bad = [i for i in items
       if not ((isinstance(i, str) and len(i.split(":")) == 3)
               or (isinstance(i, dict) and i.get("app")))]
if bad:
    sys.exit(f"entries must name an app (APP:OWNER:NAMESPACE, `*` = any): {bad}")
print(f"==> {len(items)} source(s): " + ", ".join(map(str, items)))
PY

# The image half, plus the box's SOURCES_URL — WATCHLIST_URL is how this script says
# "that mode, not yours": deploy.sh writes the line and drops any COMPOSE_FILE overlay,
# so a box that was on the worker converges here in one run instead of silently staying
# on the worker's list.
WATCHLIST_URL=file:///data/sources.json ./deploy.sh "${args[@]}"
run gcloud compute scp "$SOURCES" "$INSTANCE:~/sources.json" --zone="$ZONE"
# Into the volume, since /data in the container IS the volume. No restart: `watch`
# re-reads the file every SOURCES_REFRESH seconds and converges in place — it may log
# one "sources fetch failed" first, in the window between the container starting and
# this copy landing.
run gcloud compute ssh "$INSTANCE" --zone="$ZONE" --command='
  docker compose cp ~/sources.json watch:/data/sources.json &&
  docker compose exec -T watch cat /data/sources.json'
