# `embeddings.py` refactor plan

Split the ~1960-line `quickbeam/embeddings.py` into a coherent `quickbeam/ingest/`
subpackage. `embeddings.py` is really *the offline ingestion engine*, shared by two
drivers: the `quickbeam build` CLI **and** `watcher.py` (which imports ~15 names from
it). The refactor is behavior-preserving; `embeddings.py` stays as a thin re-export
facade so no existing importer changes.

## Decisions (locked)

- **Package name:** `quickbeam/ingest/` — named for what it is (the ingestion engine
  both `build` and `watch` sit on), not the CLI verb.
- **Compat strategy:** `embeddings.py` becomes a ~15-line facade re-exporting the
  public API. `watcher.py`, `pull.py`, `server.py`, `prebake.py`, `cli.py` all keep
  working untouched. Migrating them to direct `quickbeam.ingest.*` paths is optional
  follow-up, not part of this refactor.

## Target layout

```
quickbeam/
  ingest/
    __init__.py        # public API re-exports (the compat surface, defines __all__)
    build.py    ~230   # parse_args + main  → the `quickbeam build` CLI driver
    checkpoint.py ~55  # _load/_save_checkpoint, _save_role_map
    identity.py ~120   # _str_to_uuid, _track_id, matryoshka (deterministic id + vector transform)
    embed.py    ~230   # ResilientEmbedder, _build_text_embedding, ensure_indexes,
                       #   compose_document_text, _embed_and_upload
    umap.py     ~445   # write_umap_coords, _shape_map_track
    commits.py  ~130   # _unwrap_commit, tombstone_*, resolve_tip_commit, point-id helpers
    sources/           # ── where raw data comes from (network IO) ──
      __init__.py
      subgraph.py ~75  # event queries + the query-string constants
      ipfs.py    ~155  # b58 / _cid_to_path / _fetch_json / fetch_all_ipfs
    graph/             # ── the typed-graph domain: model + joins ──
      __init__.py
      projection.py ~200  # ROOT_PROFILES, _load_profiles, node helpers, _walk_graph,
                          #   _project, + new shared _index_chunks/_build_adj/_project_records
      bundle.py  ~110  # build_bundle_joined_data
      view.py    ~340  # _DSU, _alias_index, _resolve_endpoint, _fuse_nodes, build_view_joined_data
  embeddings.py  ~15   # thin back-compat facade: re-exports from quickbeam.ingest
```

`sources/` = external acquisition (The Graph + IPFS); `graph/` = the graph model and
the join/fusion logic that turns it into documents. Leaf primitives that are neither
(embed, umap, commits, checkpoint, identity) stay at `ingest/` top-level. Two subdirs,
one level deep. No file exceeds ~445 lines.

## Dependency layering (a clean DAG, no cycles)

```
identity ─┐
sources ──┼─► commits ──► graph ──► build ──► __init__ (facade)
          └────────────────────────┘
embed ◄── identity
```

- `commits` imports `sources.ipfs` + `identity` + `objects`
- `graph/bundle` + `graph/view` import `sources`, `graph/projection`, `commits`, `identity`, `_identity`
- `embed` imports `identity` + `roles`
- `build` imports everything; `__init__` re-exports.

As long as extraction goes leaf-first, every intermediate state compiles.

## Two real wins beyond relocation

1. **Dedup the join generators.** `build_bundle_joined_data` and
   `build_view_joined_data` independently reimplement: index chunks →
   `nodes_by_id`/`id_to_key`, build undirected adjacency, project-per-profile →
   records. Factor `_index_chunks()`, `_build_adj()`,
   `_project_records(profiles, nodes, adj, meta, key_fn)` into `graph/projection.py`.
   Removes ~40–50 lines and the parallel-maintenance drift.
2. **`matryoshka` gets a documented, stable home** (`ingest/identity.py`) but stays
   reachable as `quickbeam.embeddings.matryoshka` via the facade — the README calls
   that path out as the canonical transform the pull-client must reuse.

## Execution order (each step independently compiles + ships)

Leaf-first, so no half-broken intermediate state:

1. Scaffold `ingest/` + `sources/` + `graph/` with empty `__init__`s.
2. `identity.py` ← move ids + matryoshka; `embeddings.py` re-imports.
3. `sources/ipfs.py`, `sources/subgraph.py`.
4. `checkpoint.py`.
5. `embed.py`.
6. `umap.py`.
7. `commits.py`.
8. `graph/projection.py` (+ introduce the shared `_index_chunks`/`_build_adj`/`_project_records`).
9. `graph/bundle.py`, `graph/view.py` (rewire onto the shared helpers — the dedup lands here).
10. `ingest/build.py` ← `parse_args` + `main`.
11. Collapse `embeddings.py` to the facade; populate `ingest/__init__.__all__`.
12. Update README's module references (docs that name `quickbeam.embeddings._foo`).

## Verification gate (after EVERY step)

- `py_compile` the package
- import the three entrypoints: `quickbeam.embeddings`, `quickbeam.watcher`, `quickbeam.cli`
- run existing tests: `test_objects`, `test_roles`, `pipelines/test_robinhood`
- `quickbeam build --help` / `quickbeam watch --help` parse

Because the facade preserves the import surface, any breakage surfaces immediately as
an ImportError, not a silent runtime failure.

## Out of scope (flagged, not doing)

- `cdn.py` has its own UMAP fit (`fit_umap`, ~line 155) — a second UMAP
  implementation. Converging it onto `ingest/umap.py` is a separate change.
- Moving `watcher.py` under `ingest/watch.py` — it's a driver like `build.py`; leave
  it as a top-level sibling for now.

## Public API surface (what the facade must re-export)

Names currently imported from `quickbeam.embeddings` by other modules:

- `watcher.py`: `MODEL_DIM_MAP`, `_load_checkpoint`, `_save_checkpoint`,
  `_save_role_map`, `_init_embed_engine`, `_load_profiles`,
  `build_bundle_joined_data`, `build_view_joined_data`, `_embed_and_upload`,
  `ensure_indexes`, `matryoshka`, `resolve_tip_commit`, `tombstone_commit_delta`,
  `write_umap_coords`, `_str_to_uuid`
- `prebake.py`: `_init_embed_engine`, `matryoshka`, `ensure_indexes`, `_str_to_uuid`
- `cli.py`: `main`
- plus `matryoshka` referenced as the canonical transform by `pull.py`/`server.py`/`mcp_server.py`
