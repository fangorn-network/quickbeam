"""
Back-compat facade for the ingestion engine
"""
from quickbeam.ingest.identity import _str_to_uuid, _track_id, matryoshka
from quickbeam.ingest.checkpoint import (
    _load_checkpoint, _save_checkpoint, _save_role_map,
)
from quickbeam.ingest.embed import (
    MODEL_DIM_MAP, _build_text_embedding, _is_gpu_oom, ResilientEmbedder,
    _init_embed_engine, ensure_indexes, compose_document_text, _embed_and_upload,
)
from quickbeam.ingest.sources.subgraph import (
    _query_subgraph_async, _fetch_all_events_async, _fetch_all_events_global,
)
from quickbeam.ingest.sources.ipfs import (
    _b58encode, _cid_to_path, _fetch_json, fetch_all_ipfs,
)
from quickbeam.ingest.umap import write_umap_coords, _shape_map_track
from quickbeam.ingest.commits import (
    _unwrap_commit, _blob_point_ids, collect_tombstone_ids,
    tombstone_commit_delta, resolve_tip_commit,
)
from quickbeam.ingest.graph.projection import (
    ROOT_PROFILES, _load_profiles, _node_key, _node_label, _node_content,
    _group_key, _walk_graph, _project, _index_nodes, _build_adj, _project_records,
)
from quickbeam.ingest.graph.bundle import build_bundle_joined_data
from quickbeam.ingest.graph.view import (
    build_view_joined_data, _DSU, _alias_index, _resolve_endpoint, _fuse_nodes,
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
    # sources
    "_query_subgraph_async", "_fetch_all_events_async", "_fetch_all_events_global",
    "_b58encode", "_cid_to_path", "_fetch_json", "fetch_all_ipfs",
    # umap
    "write_umap_coords", "_shape_map_track",
    # commits
    "_unwrap_commit", "_blob_point_ids", "collect_tombstone_ids",
    "tombstone_commit_delta", "resolve_tip_commit",
    # graph projection
    "ROOT_PROFILES", "_load_profiles", "_node_key", "_node_label", "_node_content",
    "_group_key", "_walk_graph", "_project", "_index_nodes", "_build_adj", "_project_records",
    # graph joins
    "build_bundle_joined_data", "build_view_joined_data",
    "_DSU", "_alias_index", "_resolve_endpoint", "_fuse_nodes",
    # build CLI
    "parse_args", "main",
]


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
