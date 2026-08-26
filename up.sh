#!/usr/bin/env bash
# Bring the stack up, with the GPU overlay when this host can actually pass a GPU
# through. Everything after the script name is handed to `docker compose` ahead of
# `up`, so extra overlays still work:
#
#   ./up.sh
#   ./up.sh -f docker-compose.local.yml
#
# Detection is two-sided on purpose. A driver alone is not enough — without the
# nvidia container runtime registered with Docker, `gpus: all` fails the container
# at start; and the runtime alone is not enough on a box whose GPU is absent or
# busy. Anything less than both means we build the CPU image, which still works,
# just slower.
set -euo pipefail
cd "$(dirname "$0")"

files=(-f docker-compose.yml)
if nvidia-smi -L >/dev/null 2>&1 \
   && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
  echo "==> GPU detected — building the CUDA image for \`watch\`"
  files+=(-f docker-compose.gpu.yml)
else
  echo "==> no usable GPU (driver + nvidia container runtime) — CPU image"
fi

exec docker compose "${files[@]}" "$@" up -d --build
