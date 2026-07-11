"""Composed View join — multi-source fusion (Phase 1).

A bundle is one publisher's graph. A *view* fuses several publishers' graphs into one,
joining on global identity (Entity URI + namespaced aliases + asserted `sameAs`
linksets) — deterministically, no ML. Where the bundle path streams one manifest at a
time, fusion is inherently cross-source, so the view path holds all sources' nodes at
once and yields a single fused record set.
"""
import re

from quickbeam.ingest.commits import _unwrap_commit
from quickbeam.ingest.graph.projection import _build_adj, _index_nodes, _project_records
from quickbeam.ingest.sources.ipfs import _cid_to_path, fetch_all_ipfs
from quickbeam.ingest.sources.subgraph import (
    _fetch_all_events_async, _fetch_all_events_global,
)


class _DSU:
    """Tiny union-find. Roots are the lexicographically-smallest member so a fused
    cluster's canonical key is stable across runs (deterministic point ids)."""
    def __init__(self):
        self._p = {}

    def find(self, x):
        p = self._p
        p.setdefault(x, x)
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:  # path compression
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        self._p[rb] = ra


_NS_LOCAL_ID = re.compile(r"^[a-z][a-z0-9]*:.+$")  # `tribe:10020845`, `gplace:ChIJ…`


def _alias_index(nodes_by_id):
    """alias string -> a node key that carries it (first wins). Lets a linkset
    endpoint expressed as an alias (`isrc:…`) resolve to an actual fused node.

    A node's own local id is ALSO indexed when it's already namespaced
    (`<ns>:<value>`, e.g. an event's `tribe:10020845`) — that makes every node
    addressable as a linkset endpoint without minting an alias on the node itself.
    Crucially this feeds *endpoint resolution only*, not `_fuse_nodes`' union-find,
    so a node stays pointable (`hostedAt` edge target/source) without becoming a
    fusion join key — the distinction that keeps foreign-key edges from over-merging."""
    idx = {}
    for key, node in nodes_by_id.items():
        for al in (node.get("aliases") or []):
            idx.setdefault(al, key)
        lid = node.get("id")
        if isinstance(lid, str) and _NS_LOCAL_ID.match(lid):
            idx.setdefault(lid, key)
    return idx


def _resolve_endpoint(endpoint, nodes_by_id, alias_idx):
    """Map a linkset endpoint (an Entity URI or a `namespace:value` alias) to a
    fused node key, or None when it points outside this view's loaded data."""
    if not isinstance(endpoint, str) or not endpoint:
        return None
    if endpoint in nodes_by_id:          # Entity URI naming a loaded node
        return endpoint
    return alias_idx.get(endpoint)       # namespaced alias → the node carrying it


def _fuse_nodes(nodes_by_id, extra_unions=()):
    """Union-find over the shared global key: two nodes collapse to one cluster
    when they share an alias (e.g. the same `isrc:`). Identical Entity URIs have
    already collapsed via dict keying. `extra_unions` is a list of (keyA, keyB)
    pairs from asserted `sameAs` linkset edges (Phase 2) — merged into the SAME
    clusters as shared ids. Returns (dsu, merged_by_canonical_key), where each
    merged node unions its members' fields (first-writer-wins) and aliases, and
    is re-keyed to the cluster's canonical Entity URI."""
    dsu = _DSU()
    alias_owner = {}
    for key, node in nodes_by_id.items():
        dsu.find(key)  # register every node, even alias-less ones
        for al in (node.get("aliases") or []):
            prev = alias_owner.get(al)
            if prev is not None:
                dsu.union(prev, key)
            else:
                alias_owner[al] = key

    for a, b in extra_unions:  # asserted sameAs equivalences
        dsu.union(a, b)

    merged = {}
    for key, node in nodes_by_id.items():
        c = dsu.find(key)
        m = merged.get(c)
        if m is None:
            merged[c] = {
                "id": node.get("id"),
                "type": node.get("type"),
                "entityUri": c,
                "aliases": list(node.get("aliases") or []),
                "fields": dict(node.get("fields") or {}),
            }
        else:
            for fk, fv in (node.get("fields") or {}).items():
                m["fields"].setdefault(fk, fv)
            for al in (node.get("aliases") or []):
                if al not in m["aliases"]:
                    m["aliases"].append(al)
            if not m.get("type"):
                m["type"] = node.get("type")
    return dsu, merged


async def build_view_joined_data(args, view_schema_id, profiles, completed_manifest_cids=None):
    """Fuse a Composed View's source datasources into one graph and project it.

    Yields exactly one (view_manifest_cid, records) pair — the view is treated as
    a single unit of work for checkpointing, keyed on its own manifest CID.
    """
    from quickbeam._identity import resource_id, norm_hex
    completed = completed_manifest_cids or set()
    defaults = {"max_depth": args.max_depth, "label_cap": args.label_cap, "node_cap": args.node_cap}
    gw_headers = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}

    # ── 1. Resolve the view artifact → its latest manifest → source set ──
    print(f"\n[View] Resolving view schema {view_schema_id}...")
    publishes, updates = await _fetch_all_events_async(
        args.subgraph_url, args.graph_api_key, view_schema_id, args.page_size)
    view_events = publishes + updates
    if not view_events:
        print(f"[View] No manifests for view schema {view_schema_id}.")
        return
    view_ev = max(view_events, key=lambda e: int(e.get("blockNumber", 0)))
    view_mcid = view_ev["manifestCid"]
    if view_mcid in completed:
        print(f"[View] {_cid_to_path(view_mcid)[:16]}... already embedded.")
        return
    vman = (await fetch_all_ipfs([view_mcid], args.ipfs_gateway, args.ipfs_timeout,
                                 args.concurrency, desc="View Manifest", headers=gw_headers)).get(view_mcid)
    # A view tip may itself be a commit (its parents are the fused source tips,
    # slice 4); unwrap to the view manifest.
    vman, _view_commit = await _unwrap_commit(args, vman, gw_headers) if vman else (None, None)
    if not vman or vman.get("kind") != "view":
        print(f"[View] {_cid_to_path(view_mcid)!r} is not a view manifest: {str(vman)[:120]!r}")
        return
    sources = {norm_hex(s) for s in vman.get("sources", [])}
    if not sources:
        print("[View] view declares no sources.")
        return
    link_ids = {norm_hex(l) for l in vman.get("linksets", [])}
    # Phase 4 trust gate; for now honor a minConfidence floor if the view carries one.
    min_conf = (vman.get("trust") or {}).get("minConfidence")
    print(f"[View] fusing {len(sources)} source(s) + {len(link_ids)} linkset(s)"
          + (f"; minConfidence={min_conf}" if min_conf is not None else ""))

    # ── 2. Discover each source/linkset's latest manifest → resourceId match ──
    # A source is named by resourceId = keccak(owner, schemaId, datasetName), which
    # the subgraph does not index. If the view recorded the backing schemaIds
    # (ViewManifest.sourceSchemas), query those schemas directly — cheap. Fall back
    # to the whole-history scan only for sources NOT covered (e.g. foreign sources
    # whose schemaId the view didn't record), so a fully-hinted view never scans.
    wanted = sources | link_ids
    best = {}  # resourceId -> (blockNumber, manifestCid)

    def _absorb(events):
        for ev in events:
            try:
                rid = norm_hex(resource_id(ev["owner"], ev["schemaId"], ev["nameHash"], is_hash=True))
            except Exception:
                continue
            if rid not in wanted:
                continue
            bn = int(ev.get("blockNumber", 0))
            cur = best.get(rid)
            if cur is None or bn > cur[0]:
                best[rid] = (bn, ev["manifestCid"])

    schema_ids = {norm_hex(s) for s in (vman.get("sourceSchemas") or [])}
    for sid in schema_ids:
        p, u = await _fetch_all_events_async(args.subgraph_url, args.graph_api_key, sid, args.page_size)
        _absorb(p + u)
    if schema_ids:
        print(f"  ↳ view schema hint: resolved {len(set(best) & wanted)}/{len(wanted)} source(s) via {len(schema_ids)} per-schema query(ies)")

    # Global fallback: only if the hint left something unresolved (or was absent).
    if wanted - set(best):
        if schema_ids:
            print(f"  ↳ {len(wanted - set(best))} source(s) not covered by the schema hint — scanning full history")
        g_pub, g_upd = await _fetch_all_events_global(args.subgraph_url, args.graph_api_key, args.page_size)
        _absorb(g_pub + g_upd)

    if not (set(best) & sources):
        print("[View] none of the view's sources resolved to a manifest.")
        return
    missing = wanted - set(best)
    if missing:
        print(f"[View] {len(missing)} declared source/linkset(s) had no manifest and were skipped.")

    # ── 3. Fetch every source manifest's chunks → one global node index + edges ──
    nodes_by_id = {}
    edges_global = []  # (from_key, to_key), already resolved onto global keys
    for rid in (s for s in sources if s in best):
        _bn, mcid = best[rid]
        m = (await fetch_all_ipfs([mcid], args.ipfs_gateway, args.ipfs_timeout,
                                  args.concurrency, desc=f"  Src {rid[:10]}", headers=gw_headers)).get(mcid)
        if not m or m.get("kind") != "bundle":
            print(f"[View] source {rid[:10]} manifest {_cid_to_path(mcid)!r} not a bundle — skipped.")
            continue
        node_cids = [c["dataCid"] for c in m.get("nodeChunks", [])]
        edge_cids = [c["dataCid"] for c in m.get("edgeChunks", []) if c.get("dataCid")]
        chunks = await fetch_all_ipfs(node_cids + edge_cids, args.ipfs_gateway, args.ipfs_timeout,
                                      args.concurrency, desc="  Chunks", headers=gw_headers)
        # Resolve local id -> global key PER SOURCE: two publishers can reuse the
        # same local node id, so they must not collide before the union-find joins
        # on global identity (id_to_key is fresh per source).
        _, id_to_key = _index_nodes(node_cids, chunks, into=nodes_by_id)
        for ecid in edge_cids:
            for e in (chunks.get(ecid) or []):
                edges_global.append((id_to_key.get(e["from"], e["from"]),
                                     id_to_key.get(e["to"], e["to"])))

    if not nodes_by_id:
        print("[View] sources resolved but produced no nodes.")
        return

    # ── 3b. Ingest the view's linksets (Phase 2): asserted cross-edges over global
    #        identity. `sameAs` becomes a union; any other rel becomes a graph edge.
    #        Endpoints resolve to a fused node by Entity URI or by namespaced alias;
    #        a link to an entity outside this view's loaded data is dropped. ──
    alias_idx = _alias_index(nodes_by_id)
    same_as = []   # (keyA, keyB) equivalences fed into the union-find
    n_links = n_skipped = 0
    for rid in (l for l in link_ids if l in best):
        _bn, mcid = best[rid]
        m = (await fetch_all_ipfs([mcid], args.ipfs_gateway, args.ipfs_timeout,
                                  args.concurrency, desc=f"  Link {rid[:10]}", headers=gw_headers)).get(mcid)
        if not m or m.get("kind") != "linkset":
            print(f"[View] linkset {rid[:10]} manifest {_cid_to_path(mcid)!r} not a linkset — skipped.")
            continue
        link_cids = [c["dataCid"] for c in m.get("linkChunks", []) if c.get("dataCid")]
        lchunks = await fetch_all_ipfs(link_cids, args.ipfs_gateway, args.ipfs_timeout,
                                       args.concurrency, desc="  Links", headers=gw_headers)
        for lcid in link_cids:
            for link in (lchunks.get(lcid) or []):
                if min_conf is not None and link.get("confidence") is not None \
                        and link["confidence"] < min_conf:
                    n_skipped += 1
                    continue
                a = _resolve_endpoint(link.get("from"), nodes_by_id, alias_idx)
                b = _resolve_endpoint(link.get("to"), nodes_by_id, alias_idx)
                if a is None or b is None:
                    n_skipped += 1
                    continue
                if link.get("rel") == "sameAs":
                    same_as.append((a, b))
                else:
                    edges_global.append((a, b))
                n_links += 1
    if link_ids:
        print(f"[View] applied {n_links} link(s) ({len(same_as)} sameAs); skipped {n_skipped}.")

    # ── 4. Union-find: collapse cross-source nodes sharing a global key OR an
    #        asserted sameAs ──
    dsu, merged = _fuse_nodes(nodes_by_id, extra_unions=same_as)
    print(f"[View] fused {len(nodes_by_id)} nodes → {len(merged)} entities.")

    # ── 5. Undirected adjacency over canonical cluster keys ──
    adj = _build_adj((dsu.find(frm), dsu.find(to)) for frm, to in edges_global)

    # ── 6. Project per profile over the fused graph ──
    meta = {"manifestCid": view_mcid, "owner": view_ev.get("owner"), "name": view_ev.get("name")}
    records = _project_records(profiles, merged, adj, meta, defaults,
                               prefer_key="entityUri", log_prefix="[View] ")

    if records:
        yield view_mcid, records
    else:
        print("[View] fused graph produced no records.")
