#!/usr/bin/env bash
# Deploy the quickbeam image to the shared GCE instance, watching the REGISTRY
# WORKER's /watchlist — views created from the website are the source of truth.
# For a static list on the box instead, use ./deploy-sources.sh.
# Run from repo root:  ./deploy.sh [--env] [--fresh] [--dry-run]
#
# Wraps the three-command redeploy in DOCKER-README.md ("Redeploying a code
# change"): build here, push to Artifact Registry, then tell the box to pull.
# The box never gets the source — only docker-compose.yml and .env — because
# an e2-medium cannot afford to build the ONNX stack.
set -euo pipefail
cd "$(dirname "$0")"

INSTANCE=${INSTANCE:-quickbeam-1}
ZONE=${ZONE:-us-east4-a}

PUSH_ENV=0
FRESH=0
DRY=0
for a in "$@"; do
  case "$a" in
    # Copy .env too. Off by default: it carries ETH_PRIVATE_KEY and
    # QDRANT_API_KEY, and the box's copy may have been edited in place.
    # Needed on a first deploy, or after rotating a key / changing a port.
    --env)     PUSH_ENV=1 ;;
    # Deploy as if the box had never run: drop the qdrant and data volumes, so it
    # comes up with no collection, no ingest checkpoint and no baked CDN shards and
    # re-embeds everything from chain. Slow (a full --from-block replay + one
    # `fangorn read` and embed per pair), so it is opt-in, not the default.
    --fresh)   FRESH=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "usage: $0 [--env] [--fresh] [--dry-run]" >&2; exit 2 ;;
  esac
done

run() { if [[ $DRY -eq 1 ]]; then echo "  + $*"; else "$@"; fi; }

# IMAGE lives in .env because that is what docker-compose.yml reads. Deploying
# with it unset is THE failure mode of this flow: compose falls back to
# quickbeam:local, which does not exist on the box, so it tries to build from a
# directory with no Dockerfile — and the error points at the build, not at the
# missing variable. Catch it here instead.
[[ -f .env ]] || { echo "no .env — copy .env.example and set IMAGE" >&2; exit 1; }
# `|| true` because pipefail turns grep's "no match" into a silent exit 1,
# which would skip the explanatory error below.
IMG=$(grep -E '^IMAGE=' .env | tail -1 | cut -d= -f2- || true)
[[ -n "$IMG" ]] || { echo "IMAGE is unset in .env (compose would fall back to quickbeam:local)" >&2; exit 1; }
[[ "$IMG" != *"quickbeam:local"* ]] || { echo "IMAGE points at a local build, not Artifact Registry: $IMG" >&2; exit 1; }

# The mode lives in ONE variable: the box's SOURCES_URL. It is rewritten on every
# deploy rather than assumed, because deploy-sources.sh points it at a file and a box
# left in that mode ignores the worker silently — it keeps serving whatever list it
# has and nothing anywhere says why. COMPOSE_FILE goes for the same reason: a
# standalone overlay pinned there re-applies the file mode under a bare
# `docker compose up -d`.
# WATCHLIST_URL, when set, is deploy-sources.sh calling in to reuse the image half of
# this script while owning the mode itself — so the .env guard below is skipped.
if [[ -n "${WATCHLIST_URL:-}" ]]; then
  SOURCES_URL=$WATCHLIST_URL
else
  SOURCES_URL=$(grep -E '^SOURCES_URL=' .env | tail -1 | cut -d= -f2- || true)
  [[ -n "$SOURCES_URL" ]] || { echo "SOURCES_URL is unset in .env (needs the worker's /watchlist URL)" >&2; exit 1; }
  case "$SOURCES_URL" in
    file://*) echo "SOURCES_URL in .env is a file:// list — that is ./deploy-sources.sh's job, not this script's" >&2; exit 1 ;;
    http://*|https://*) ;;
    # A bare host is what you get from copying the worker's hostname out of the
    # dashboard. urlopen() rejects it with "unknown url type" on every poll, which the
    # watcher reports as a fetch failure and then keeps its old list forever.
    *) SOURCES_URL="https://$SOURCES_URL" ;;
  esac
fi

# --fresh's remote half. Three separate stores hold ingest state and ALL of them have
# to go together: the qdrant volume (the vectors), /data/db/checkpoint.json (whose
# processed_track_ids + per-source vertex_cids make the watcher skip anything it has
# already embedded) and /data/cdn (whose manifests make append_domain skip anything
# already delivered). Wiping only qdrant leaves the other two claiming the work is
# done, and the watcher then logs "no new records" over an empty collection forever.
# `down -v` drops both named volumes in one go — see DOCKER-README.md's Teardown.
FRESH_CMD=""
if [[ $FRESH -eq 1 ]]; then
  # Checked on the BOX, not here, because .env only ships with --env — the local copy
  # is not what the watcher reads. Aborts before the wipe, never after.
  # ponytail: FROM_BLOCK is the only rebuild path while the watch list is wildcard
  # (`*:*`) sources — those get no startup seed read, so their pairs are only
  # discovered by replaying StateCommitted history. Relax this to a warning if pinned
  # `owner:namespace` entries ever come back, since those DO seed themselves.
  FRESH_CMD="grep -qE '^FROM_BLOCK=[1-9]' .env \
    || { echo 'FROM_BLOCK is 0/unset on the box: a wiped instance would never rebuild, because wildcard sources have no seed read. Set it in .env and redeploy with --env.' >&2; exit 1; } \
    && docker compose down -v && "
fi

# Also tag the immutable git sha. `docker compose pull` on a moving :latest
# cannot tell you which build a box is running; this leaves a tag you can roll
# back to. -dirty when the tree has uncommitted changes, so the tag never lies.
# Scoped to what the Dockerfile actually COPYs, so the tag is dirty exactly when
# the image differs from the commit — not when some unrelated doc is edited.
# --porcelain (rather than `git diff`) because it also catches an UNTRACKED file
# under quickbeam/, which `COPY quickbeam ./quickbeam` would bake in regardless.
SHA=$(git rev-parse --short HEAD)
[[ -z "$(git status --porcelain -- quickbeam pyproject.toml Dockerfile)" ]] || SHA="$SHA-dirty"
SHA_IMG="${IMG%:*}:$SHA"

echo "==> $IMG"
echo "==> $SHA_IMG"
echo "==> $INSTANCE ($ZONE)"
echo "==> watch list: $SOURCES_URL"
[[ $DRY -eq 1 ]] && echo "-- dry run, nothing will be built, pushed or restarted --"

# Asked BEFORE the build so a stray --fresh costs a keystroke, not five minutes and a
# re-embed. Skipped with no tty (CI) and on a dry run, which wipes nothing anyway.
if [[ $FRESH -eq 1 && $DRY -eq 0 ]]; then
  echo "==> FRESH: drops the qdrant + data volumes (vectors, checkpoints, shards)"
  if [[ -t 0 ]]; then
    read -r -p "    re-embed everything from chain? [y/N] " ans
    [[ $ans == [yY] ]] || { echo "aborted"; exit 1; }
  fi
fi

run docker build -t "$IMG" -t "$SHA_IMG" .
run docker push "$IMG"
run docker push "$SHA_IMG"

# docker-compose.yml goes every time — it is small, and copying it
# unconditionally beats asking whether it changed since the last deploy.
files=(docker-compose.yml)
[[ $PUSH_ENV -eq 1 ]] && files+=(.env)
run gcloud compute scp "${files[@]}" "$INSTANCE:~/" --zone="$ZONE"

# Recreates only the services whose image actually changed; the qdrant and data
# volumes are untouched, so the collection and baked shards survive — unless --fresh
# put a `down -v` in front, which is exactly what drops them.
#
# The prune is not optional housekeeping. IMAGE is a moving :latest, so every pull
# leaves the previous ~2.8GB image untagged — invisible to plain `docker images`,
# which is why a 20GB box fills up while it still reports 3GB of images. Six of them
# had accumulated (14.8GB) by 2026-08-20. Dangling only: the image the stack now runs
# is tagged, and rollback images live in Artifact Registry, not here.
run gcloud compute ssh "$INSTANCE" --zone="$ZONE" \
  --command="sed -i -e '/^SOURCES_URL=/d' -e '/^COMPOSE_FILE=/d' .env \
    && echo 'SOURCES_URL=$SOURCES_URL' >> .env \
    && ${FRESH_CMD}docker compose pull && docker compose up -d && docker image prune -f && docker compose ps"

if [[ $DRY -eq 0 ]]; then
  echo
  echo "deployed $SHA. Roll back with:"
  echo "  ssh the box and run: docker compose pull ${SHA_IMG%:*}:<older-sha>"
  echo "cdn restarts until watch bakes the first domain — see DOCKER-README.md."
  [[ $FRESH -eq 1 ]] && echo "fresh: the collection rebuilds by replaying from FROM_BLOCK — follow it with 'docker compose logs -f watch'."
fi
