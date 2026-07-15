"""
Back-compat facade for the ingestion engine.

Re-exports the ingest primitives from their real homes under `quickbeam.ingest.*`
so callers (and the watcher/server) can import them from one stable module. The data
model is owner:namespace: a namespace's graph is read off-chain via `fangorn read`,
tailed via `fangorn subscribe`, projected, and embedded — there is no subgraph, IPFS
gateway, or bundle schema.
"""
from quickbeam.ingest.identity import _str_to_uuid, _track_id, matryoshka
from quickbeam.ingest.checkpoint import (
    _load_checkpoint, _save_checkpoint, _save_role_map,
)
from quickbeam.ingest.embed import (
    MODEL_DIM_MAP, _build_text_embedding, _is_gpu_oom, ResilientEmbedder,
    _init_embed_engine, ensure_indexes, compose_document_text, _embed_and_upload,
)
from quickbeam.ingest.sources.fangorn import (
    parse_sources, read_source, read_head, subscribe_cmd,
)
from quickbeam.ingest.umap import write_umap_coords, _shape_map_track
from quickbeam.ingest.graph.projection import (
    load_profiles, _load_profiles, project_source,
    _node_key, _node_label, _node_content, _group_key, _walk_graph, _project,
    _index_nodes, _build_adj, _project_records,
)
from quickbeam.ingest.build import parse_args, main

__all__ = [
    # identity + vector transform
    "_str_to_uuid", "_track_id", "matryoshka",
    # checkpoint
    "_load_checkpoint", "_save_checkpoint", "_save_role_map",
    # embed
    "MODEL_DIM_MAP", "_build_text_embedding", "_is_gpu_oom", "ResilientEmbedder",
    "_init_embed_engine", "ensure_indexes", "compose_document_text", "_embed_and_upload",
    # owner:namespace source bridge (fangorn read/subscribe)
    "parse_sources", "read_source", "read_head", "subscribe_cmd",
    # umap
    "write_umap_coords", "_shape_map_track",
    # graph projection
    "load_profiles", "_load_profiles", "project_source",
    "_node_key", "_node_label", "_node_content", "_group_key", "_walk_graph",
    "_project", "_index_nodes", "_build_adj", "_project_records",
    # build CLI
    "parse_args", "main",
]


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
