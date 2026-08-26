# Quickbeam — one image, several entrypoints.
#
# ENTRYPOINT is the `quickbeam` CLI, so each service picks its verb as the command:
#   watch      follow a namespace's on-chain head and embed each commit
#   cdn serve  static shard delivery (what `mcp` pulls from)
#   serve      the search API a website calls
#   mcp        the agent-facing MCP server
#
# See docker-compose.yml for the wiring and deploy/cloudrun/ for the stateless half.
FROM python:3.12-slim

# Node is not optional. `quickbeam watch` shells out to the `fangorn` CLI for every
# chain read (quickbeam/ingest/sources/fangorn.py) and that CLI is a Node package.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# Provides the `fangorn` binary on PATH — the default --fangorn-bin.
RUN npm i -g @fangorn-network/sdk && npm cache clean --force

WORKDIR /app
COPY pyproject.toml ./
COPY quickbeam ./quickbeam
# cpu:   fastembed without CUDA (the box embeds deltas, not backfills — see README)
# gpu:   fastembed-gpu, i.e. onnxruntime-gpu — built by docker-compose.gpu.yml, which
#        up.sh layers in only on a host that can actually pass a GPU through.
# agent: fastmcp, which `quickbeam mcp` imports
ARG EXTRAS=cpu
RUN pip install --no-cache-dir -e ".[${EXTRAS},agent]"

# onnxruntime-gpu dynamically links the CUDA runtime (libcublasLt.so.13, libcudart.so.13,
# ...) and bundles none of it. The container runtime injects the driver's libcuda.so.1;
# the rest has to come from pip, or the CUDA provider fails to dlopen and onnxruntime
# SILENTLY FALLS BACK TO CPU — no error, just ~10x slower embedding (see gpu-env.sh).
# Take the CUDA deps from onnxruntime-gpu's OWN `cuda`/`cudnn` extras rather than naming
# nvidia-* packages here: the wheel pins the CUDA major it was built against (1.29 = CUDA
# 13, not 12), and a hand-written list silently drifts on the next fastembed-gpu bump.
# ldconfig instead of LD_LIBRARY_PATH: the wheels do not agree on a directory name
# (nvidia/cu13/lib, nvidia/cudnn/lib, ...), so glob whatever they laid down.
RUN if [ "$EXTRAS" = "gpu" ]; then \
      pip install --no-cache-dir "onnxruntime-gpu[cuda,cudnn]" \
   && python -c "import site; print(site.getsitepackages()[0])" > /tmp/sp \
   && ls -d "$(cat /tmp/sp)"/nvidia/*/lib "$(cat /tmp/sp)"/onnxruntime/capi \
        > /etc/ld.so.conf.d/nvidia-pip.conf \
   && ldconfig; \
    fi

# Python block-buffers stdout when it isn't a TTY, so a long-running container's
# progress (every `print` in the watcher) sits invisible in a buffer while stderr
# streams — which reads as "it stopped working". Line-buffer it.
ENV PYTHONUNBUFFERED=1

# Bake the ONNX model into the image. Left to run time it downloads into
# /tmp/fastembed_cache (ingest/embed.py) on every single container start.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache
RUN python -c "from fastembed import TextEmbedding; \
    TextEmbedding(model_name='nomic-ai/nomic-embed-text-v1.5', max_length=256)"

# `fangorn subscribe` writes its resume cursor to $PWD/.fangorn/ (fangorn cli.ts), so
# the working directory has to be the mounted volume or every restart replays from
# scratch.
WORKDIR /data

ENTRYPOINT ["quickbeam"]
CMD ["--help"]
