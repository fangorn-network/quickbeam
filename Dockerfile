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
# agent: fastmcp, which `quickbeam mcp` imports
RUN pip install --no-cache-dir -e ".[cpu,agent]"

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
