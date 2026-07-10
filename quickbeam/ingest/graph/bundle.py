"""Bundle join — walk one publisher's typed graph, one manifest at a time.

Yields (manifest_cid, [records]) for each pending bundle manifest. Processing one
manifest at a time keeps memory bounded regardless of collection size; chunk data is
freed before the next manifest is fetched.

A `--bundle NAME=schemaId` schema can tip either of two manifest shapes, and this
generator understands both: a `"bundle"` manifest (typed nodeChunks/edgeChunks, from
`fangorn.publisher.publishBundle`) or a `"record-set"` manifest (the flat
entries-with-dataCid shape produced by the git-native `fangorn commit`/`push` flow —
see `recordset.py`). Either way the manifest is resolved into the same node/edge
shape before projecting, so the rest of the pipeline can't tell them apart.

Parameters
  completed_manifest_cids : set of already-finished manifest CIDs to skip
  owner_filter            : set of lowercase owner addresses (None = any)
  name_filter             : set of lowercase dataset names  (None = any)
  block_gt                : if set, only query events with blockNumber > this
"""
from quickbeam.ingest.commits import _unwrap_commit
from quickbeam.ingest.graph.projection import _build_adj, _index_nodes, _project_records
from quickbeam.ingest.graph.recordset import _entries_to_nodes, _entries_to_edge_pairs, resolve_record_entries
from quickbeam.ingest.sources.ipfs import _cid_to_path, fetch_all_ipfs
from quickbeam.ingest.sources.subgraph import _fetch_all_events_async


async def build_bundle_joined_data(
    args,
    schema_id,
    profiles,
    completed_manifest_cids=None,
    owner_filter=None,
    name_filter=None,
    block_gt=None,
    edges_sink=None,
):
    completed = completed_manifest_cids or set()
    defaults = {"max_depth": args.max_depth, "label_cap": args.label_cap,
                "node_cap": args.node_cap}

    print(f"\n[Builder] Querying Subgraph for bundle ManifestPublished/Updated events...")
    publishes, updates = await _fetch_all_events_async(
        args.subgraph_url, args.graph_api_key, schema_id, args.page_size,
        block_gt=block_gt
    )

    cids_meta = {}
    for p in publishes: cids_meta[p["manifestCid"]] = p
    for u in updates:   cids_meta[u["manifestCid"]] = u

    if not cids_meta:
        return

    # Apply filter hierarchy: owner → dataset name
    if owner_filter:
        cids_meta = {c: m for c, m in cids_meta.items()
                     if m.get("owner", "").lower() in owner_filter}
    if name_filter:
        cids_meta = {c: m for c, m in cids_meta.items()
                     if m.get("name", "").lower() in name_filter}

    pending_cids = [c for c in cids_meta if c not in completed]
    if not pending_cids:
        print("[Builder] No pending manifests after filters.")
        return

    print(f"[Builder] {len(pending_cids)} pending manifests (skipped {len(cids_meta) - len(pending_cids)} completed).")

    gw_headers = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}

    # Fetch only the manifest envelopes (small JSON), not chunks yet.
    manifests = await fetch_all_ipfs(
        pending_cids, args.ipfs_gateway, args.ipfs_timeout,
        args.concurrency, desc="Bundle Manifests", headers=gw_headers
    )

    for mcid in pending_cids:
        m = manifests.get(mcid)
        # The on-chain tip is a git-native commit wrapping the tree; unwrap it first.
        m, commit = await _unwrap_commit(args, m, gw_headers) if m else (None, None)
        kind = m.get("kind") if m else None
        if kind not in ("bundle", "record-set"):
            print(f"[Builder] Skipping invalid manifest {_cid_to_path(mcid)!r}: {str(m)[:120]!r}")
            continue
        if commit:
            print(f"[Builder] tip {_cid_to_path(mcid)[:16]}... is commit "
                  f"(parents={len(commit.get('parents', []))}, tree={_cid_to_path(commit['tree'])[:12]}...)")

        meta = cids_meta[mcid]

        if kind == "record-set":
            entries = m.get("entries", [])
            if not entries:
                print(f"[Builder] Skipping manifest {_cid_to_path(mcid)!r} — no entries")
                continue

            print(f"[Builder] Resolving {len(entries)} record blobs for {_cid_to_path(mcid)[:16]}...")
            resolved = await resolve_record_entries(
                entries, args.ipfs_gateway, args.ipfs_timeout, args.concurrency,
                gw_headers, desc="  Records")

            # record-set data is inherently single-shaped (one Fangorn schema = one
            # record shape), so every resolved record is tagged with the driving
            # profile's root_type — there's no per-node type to read off the chain.
            record_type = profiles[0]["root_type"] if profiles else "Record"
            nodes = _entries_to_nodes(resolved, record_type)
            nodes_by_id = {n["id"]: n for n in nodes}
            edge_pairs = list(_entries_to_edge_pairs(nodes))

            if edges_sink is not None:
                for child_id, parent_id in edge_pairs:
                    edges_sink.append({"rel": "parentOf", "from": parent_id, "to": child_id,
                                       "fromType": record_type, "toType": record_type})

            adj = _build_adj(edge_pairs)

            records = _project_records(
                profiles, nodes_by_id, adj, meta, defaults, prefer_key="id",
                log_prefix=f"[Builder] Manifest {_cid_to_path(mcid)[:16]}... ")

            del resolved, nodes, nodes_by_id, edge_pairs, adj
            import gc; gc.collect()
        else:
            node_cids = [c["dataCid"] for c in m.get("nodeChunks", [])]
            edge_cids = [c["dataCid"] for c in m.get("edgeChunks", []) if c.get("dataCid")]
            if not edge_cids:
                print(f"[Builder] Skipping manifest {_cid_to_path(mcid)!r} — no edge chunks")
                continue

            # Fetch only this manifest's chunks, then free them before moving on.
            # This bounds RAM to one manifest's data at a time.
            print(f"[Builder] Fetching {len(node_cids) + len(edge_cids)} chunks for {_cid_to_path(mcid)[:16]}...")
            chunks = await fetch_all_ipfs(
                node_cids + edge_cids, args.ipfs_gateway, args.ipfs_timeout,
                args.concurrency, desc="  Chunks", headers=gw_headers
            )

            nodes_by_id, id_to_key = _index_nodes(node_cids, chunks)
            edges = []
            for ecid in edge_cids:
                edges.extend(chunks.get(ecid) or [])

            # Surface this manifest's typed edges to the caller (the watcher ships them
            # to the CDN's relational axis). Endpoints are kept as the publisher-local
            # ids, which is exactly what a record's `track_id` uses — so the delivered
            # edges join the delivered records by id in the pull-client's `neighbors`.
            if edges_sink is not None:
                for e in edges:
                    if e.get("from") and e.get("to") and e.get("rel"):
                        edges_sink.append({k: e[k] for k in
                                           ("rel", "from", "to", "fromType", "toType")
                                           if e.get(k) is not None})

            # Undirected adjacency, reused across every profile — a Place must reach the
            # artists/events on either side of its edges.
            adj = _build_adj((id_to_key.get(e["from"], e["from"]),
                              id_to_key.get(e["to"], e["to"])) for e in edges)

            records = _project_records(
                profiles, nodes_by_id, adj, meta, defaults, prefer_key="id",
                log_prefix=f"[Builder] Manifest {_cid_to_path(mcid)[:16]}... ")

            del chunks, nodes_by_id, id_to_key, edges, adj
            import gc; gc.collect()

        if records:
            yield mcid, records
        else:
            print(f"[Builder] Manifest {_cid_to_path(mcid)!r} produced no records — skipping.")
