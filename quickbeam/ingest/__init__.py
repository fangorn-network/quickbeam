"""Offline ingestion engine for quickbeam (the `build` CLI + `watch` daemon).

Modules are grouped by concern:
  sources/   external data acquisition (The Graph subgraph + IPFS)
  graph/     the typed-graph model and the join/fusion projections
  embed, umap, commits, checkpoint, identity  leaf primitives
  build      the `quickbeam build` CLI driver

Importing this package (via any submodule) applies the process-wide setup the old
monolithic `embeddings.py` did at import time: a UTF-8 console on Windows and a
certifi CA bundle for TLS. Kept here so every consumer (build, watch, server) gets
it regardless of which submodule they import first.
"""
import io
import os
import sys

import certifi

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

os.environ['SSL_CERT_FILE'] = certifi.where()
