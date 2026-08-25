#!/usr/bin/env bash
# Deploy the quickbeam image to the shared GCE instance.
# Run from repo root:  ./deploy.sh [--env] [--dry-run]
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
DRY=0
for a in "$@"; do
  case "$a" in
    # Copy .env too. Off by default: it carries ETH_PRIVATE_KEY and
    # QDRANT_API_KEY, and the box's copy may have been edited in place.
    # Needed on a first deploy, or after rotating a key / changing a port.
    --env)     PUSH_ENV=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "usage: $0 [--env] [--dry-run]" >&2; exit 2 ;;
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
# IMG=$(grep -E '^IMAGE=' .env | tail -1 | cut -d= -f2- || true)
IMG="us-east4-docker.pkg.dev/lucky-lead-489114-d7/quickbeam/quickbeam:latest"
[[ -n "$IMG" ]] || { echo "IMAGE is unset in .env (compose would fall back to quickbeam:local)" >&2; exit 1; }
[[ "$IMG" != *"quickbeam:local"* ]] || { echo "IMAGE points at a local build, not Artifact Registry: $IMG" >&2; exit 1; }

# Also tag the immutable git sha. `docker compose pull` on a moving :latest
# cannot tell you which build a box is running; this leaves a tag you can roll
# back to. -dirty when the tree has uncommitted changes, so the tag never lies.
# Scoped to what the Dockerfile actually COPYs, so the tag is dirty exactly when
# the image differs from the commit — not when some unrelated doc is edited.
# --porcelain (rather than `git diff`) because it also catches an UNTRACKED file
# under quickbeam/, which `COPY quickbeam ./quickbeam` would bake in regardless.
SHA=$(git rev-parse --short HEAD)
[[ -z "$(git status --porcelain -- quickbeam pyproject.toml)" ]] || SHA="$SHA-dirty"
SHA_IMG="${IMG%:*}:$SHA"

echo "==> $IMG"
echo "==> $SHA_IMG"
echo "==> $INSTANCE ($ZONE)"
[[ $DRY -eq 1 ]] && echo "-- dry run, nothing will be built, pushed or restarted --"

run docker build -t "$IMG" -t "$SHA_IMG" .
run docker push "$IMG"
run docker push "$SHA_IMG"

# docker-compose.yml goes every time — it is small, and copying it
# unconditionally beats asking whether it changed since the last deploy.
files=(docker-compose.yml)
[[ $PUSH_ENV -eq 1 ]] && files+=(.env)
run gcloud compute scp "${files[@]}" "$INSTANCE:~/" --zone="$ZONE"

# Recreates only the services whose image actually changed; the qdrant and data
# volumes are untouched, so the collection and baked shards survive.
run gcloud compute ssh "$INSTANCE" --zone="$ZONE" \
  --command='docker compose pull && docker compose up -d && docker compose ps'

if [[ $DRY -eq 0 ]]; then
  echo
  echo "deployed $SHA. Roll back with:"
  echo "  ssh the box and run: docker compose pull ${SHA_IMG%:*}:<older-sha>"
  echo "cdn restarts until watch bakes the first domain — see DOCKER-README.md."
fi
