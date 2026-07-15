# """Graph projection: root profiles + the walk that turns a typed graph into documents.

# A *profile* projects one graph from a chosen root type into a distinct document,
# walking up to `max_depth` hops and folding included neighbor types into grouped,
# deduped label lists. The shared join helpers at the bottom (`_index_nodes`,
# `_build_adj`, `_project_records`) are used by BOTH the bundle walk and the view
# fusion, so the two generators build nodes/adjacency/records identically.
# """
# import json
# import os

# from quickbeam.ingest.identity import _track_id

# # ---------------------------------------------------------------------------
# # ROOT PROFILES — graph-as-source-of-truth projections
# #
# # A bundle is a graph (typed nodes + typed edges). A *profile* projects that one
# # graph from a chosen root type into a distinct document: walking the graph up to
# # `max_depth` hops and folding the neighbor entities it cares about (`include`)
# # into grouped label lists. The SAME graph yields a Track view, an Artist view, a
# # Place view, etc. — each becomes its own embedding. Add a profile here (or via
# # --profiles-file) and a new semantic view exists with no change to the graph.
# #
# # Node types are the entityTypes produced by the mb_pg registry: Artist,
# # ReleaseGroup, Release, Recording, Work, Area, Place, Event, Instrument.
# # ---------------------------------------------------------------------------

# # These are a collection of default or frequently used root profiles.
# #
# # Define your own without touching this file: write a JSON map of
# # {name: profile} and pass --profiles-file foo.json --root-profile <name>.
# # Your entries merge OVER these built-ins (same name = override). See
# # profiles.example.json. A profile's keys:
# #   root_type       node `type` that becomes one document       (required)
# #   max_depth       graph-walk depth folding neighbours in      (default --max-depth)
# #   include         list of neighbour types to fold; omit=all   (default all)
# #   content_fields  node fields folded as free text, not label  (default label only)
# #   label_cap       max folded labels per neighbour group       (default --label-cap)
# #   node_cap        max nodes visited per root                  (default --node-cap)
# # The root node's OWN fields always pass through; include/content_fields only
# # govern which neighbour text gets folded in.
# ROOT_PROFILES: dict[str, dict] = {
#     "track": {
#         "root_type": "Recording", "max_depth": 2,
#         "include": ["Artist", "Work", "Release", "ReleaseGroup", "Place", "Event", "Area"],
#     },
#     "recording": {  # alias of track for graphs that name the root "Recording"
#         "root_type": "Recording", "max_depth": 2,
#         "include": ["Artist", "Work", "Release", "ReleaseGroup", "Place", "Event", "Area"],
#     },
#     "artist": {
#         "root_type": "Artist", "max_depth": 2,
#         "include": ["Recording", "Release", "ReleaseGroup", "Work", "Place", "Event", "Area"],
#     },
#     "release": {
#         "root_type": "Release", "max_depth": 2,
#         "include": ["Artist", "Recording", "ReleaseGroup", "Work"],
#     },
#     "place": {
#         "root_type": "Place", "max_depth": 3,
#         "include": ["Artist", "Recording", "Event", "Area"],
#     },
#     "event": {
#         "root_type": "Event", "max_depth": 2,
#         "include": ["Artist", "Recording", "Place", "Area"],
#     },
#     "work": {
#         "root_type": "Work", "max_depth": 2,
#         "include": ["Artist", "Recording", "Release"],
#     },
#     # local-business graph (places_pg): one document per Business, folding in its
#     # reviews, categories, locality, and reviewers — the shape the per-bar demo
#     # shard embeds. Depth 2 reaches Business→Review→Reviewer. Nearby businesses
#     # ("Business") are deliberately NOT folded: a list of 20 neighbouring bar
#     # names is pure noise that dilutes the vector and crowds review content out of
#     # the embedding's token budget. The `near` graph edges still exist for the
#     # "nearby" UI rail — they just don't pollute the embedded text.
#     "business": {
#         "root_type": "Business", "max_depth": 2,
#         "include": ["Review", "Category", "Locality", "Reviewer", "Event"],
#     },
#     # one document per Review, so the review *body* (the high-value free-text
#     # signal — "best tacos in town") is embedded and directly searchable. Without
#     # this, a review only ever folds into its Business as a label ("<author> on
#     # <business>") and its body is invisible to vector search. Folding the body
#     # into the Business doc alone isn't enough: dozens of long reviews can't fit a
#     # single 256-token business embedding, so each review needs its own document.
#     # Depth 1 folds in the venue Business + Reviewer for context; the Review's
#     # businessId field links a hit back to its place.
#     "review": {
#         "root_type": "Review", "max_depth": 1,
#         "include": ["Business", "Reviewer"],
#     },
#     # events graph (events_pg), merged into the places graph: one document per
#     # Event, folding in its venue Business, organizer, category and locality.
#     "localevent": {
#         "root_type": "Event", "max_depth": 2,
#         "include": ["Business", "Organizer", "Category", "Locality"],
#     },
#     # Robinhood-Chain financial graph (robinhood.py): one document per tokenized
#     # equity (Asset), folding in that stock's notable on-chain transfer flow so a
#     # semantic query matches the equity WITH its live context. Depth 1 — every
#     # Transfer hangs directly off its Asset. Each Transfer also embeds as its own
#     # record via the `transfer` profile, so a query can hit the event directly.
#     "asset": {
#         "root_type": "Asset", "max_depth": 1,
#         "include": ["Transfer"],
#         # fold each Transfer's verbalized blurb, not its label (the company name)
#         "content_fields": ["text"],
#     },
#     # one document per Transfer, folding in its Asset's blurb (business profile +
#     # live stats) so a semantic query matches the flow through what the company IS.
#     "transfer": {
#         "root_type": "Transfer", "max_depth": 1,
#         "include": ["Asset"],
#         "content_fields": ["text"],
#     },
# }


# def load_profiles(args, discovered_tags: set[str] | None = None) -> list[dict]:
#     """Resolve --root-profile names (or auto-derive from what's actually in the
#     source) into a list of fully-specified profile dicts. Each carries a `name`
#     and `root_type` (a vertex tag / schemaId).

#     Unlike the old bundle model — where a root profile had to name a known,
#     curated entity type — the owner:namespace model lets any `--root-profile`
#     stand for an arbitrary vertex tag: a name not in the registry (or
#     --profiles-file) is taken literally as its own `root_type` with a one-hop,
#     unfiltered walk. With NO --root-profile at all, we auto-derive one profile
#     per distinct vertex tag present in the source (`discovered_tags`), so a
#     caller can point quickbeam at a namespace with zero schema knowledge.
#     """
#     registry = dict(ROOT_PROFILES)
#     if args.profiles_file and os.path.exists(args.profiles_file):
#         with open(args.profiles_file) as f:
#             for name, prof in (json.load(f) or {}).items():
#                 registry[name.lower()] = {**registry.get(name.lower(), {}), **prof}

#     if args.root_profile:
#         profiles = []
#         for raw in args.root_profile:
#             key = raw.strip().lower()
#             prof = {"name": key, **registry[key]} if key in registry else \
#                    {"name": key, "root_type": raw.strip()}
#             prof.setdefault("max_depth", args.max_depth)
#             prof.setdefault("include", None)
#             profiles.append(prof)
#         return profiles

#     # Zero-config default: one profile per distinct vertex tag actually present
#     # in this source, folding in whatever's within one hop (no curated `include`
#     # filter — we don't know the shape of arbitrary data ahead of time).
#     tags = sorted(discovered_tags or [])
#     return [{"name": t.lower(), "root_type": t, "max_depth": args.max_depth, "include": None}
#             for t in tags]


# # Back-compat alias — callers pre-migration imported `_load_profiles`.
# _load_profiles = load_profiles


# def _node_key(node: dict) -> str:
#     """Global join key for a node: its Entity URI when present else the raw local id. 
#     Keying the adjacency and projections on this resolves edges on the globally-unique identity rather than a
#     publisher-local id for cross-publisher linking."""
#     return node.get("entityUri") or node.get("id")


# def _node_label(node: dict) -> str:
#     """Human label for a node — title / name / label, whichever the node carries."""
#     f = node.get("fields", {}) or {}
#     for k in ("title", "name", "label"):
#         v = f.get(k)
#         if isinstance(v, str) and v.strip():
#             return v.strip()
#     return ""


# def _node_content(node: dict, extra_keys=()) -> str:
#     """Folded value for a neighbour node. Prefer a free-form *content* field (a
#     Review `body`, an event/summary `description`) so that text becomes searchable
#     when the node is folded into a root document — without this a Review folds in
#     only its "<author> on <business>" title and the body ("best tacos in town") is
#     silently dropped. Content-less nodes (Category, Locality, Reviewer) have none
#     of these fields and fall back to their title label, so they don't bloat the doc.

#     `extra_keys` (a profile's `content_fields`) are consulted first — e.g. the
#     robinhood graph carries its verbalized blurb in `text`, which would otherwise
#     lose to the label ("NVIDIA") and fold every transfer down to the company name."""
#     f = node.get("fields", {}) or {}
#     for k in (*(extra_keys or ()), "body", "summary", "description"):
#         v = f.get(k)
#         if isinstance(v, str) and v.strip():
#             return v.strip()
#     return _node_label(node)

# # normalize group keys
# def _group_key(type_name: str) -> str:
#     """Node type → camelCase plural field name. Artist→artists, Work→works,
#     Place→places, ReleaseGroup→releaseGroups."""
#     t = (type_name[:1].lower() + type_name[1:]) if type_name else type_name
#     if t.endswith("y"):
#         return t[:-1] + "ies"
#     if t.endswith(("s", "x", "z", "ch", "sh")):
#         return t + "es"
#     return t + "s"

# # walk the graph and rebuild the bundles
# def _walk_graph(root_id, adj, max_depth, node_cap):
#     """BFS from root over an (undirected) adjacency map, returning [(node_id, depth)]
#     for every reachable node within `max_depth` (excluding the root). Bounded by
#     `node_cap` so a high-degree hub can't blow up a single projection."""
#     from collections import deque
#     visited = {root_id}
#     queue = deque([(root_id, 0)])
#     collected = []
#     while queue:
#         nid, d = queue.popleft()
#         if d >= max_depth:
#             continue
#         for nb in adj.get(nid, ()):  # neighbors
#             if nb in visited:
#                 continue
#             visited.add(nb)
#             collected.append((nb, d + 1))
#             if len(collected) >= node_cap:
#                 return collected
#             queue.append((nb, d + 1))
#     return collected


# def _project(root, nodes_by_id, adj, profile, defaults):
#     """Project a root node into a profile document: walk the graph and fold the
#     included neighbors into grouped, deduped, capped label lists."""
#     rt = profile.get("root_type") or root.get("type")
#     fields = dict(root.get("fields", {}))

#     depth = int(profile.get("max_depth", defaults["max_depth"]))
#     label_cap = int(profile.get("label_cap", defaults["label_cap"]))
#     node_cap = int(profile.get("node_cap", defaults["node_cap"]))
#     include = profile.get("include")
#     include_set = set(include) if include else None

#     groups: dict = {}
#     content_fields = profile.get("content_fields") or ()
#     for nid, _depth in _walk_graph(_node_key(root), adj, depth, node_cap):
#         nb = nodes_by_id.get(nid)
#         if not nb:
#             continue
#         t = nb.get("type")
#         if include_set is not None and t not in include_set:
#             continue
#         value = _node_content(nb, content_fields)
#         if value:
#             groups.setdefault(_group_key(t), []).append(value)

#     for k, vals in groups.items():
#         fields[k] = list(dict.fromkeys(vals))[:label_cap]  # dedupe (order-preserving) + cap
#     fields["entityType"] = rt
#     return fields

# # ---------------------------------------------------------------------------
# # SHARED JOIN HELPERS — used by both the bundle walk and the view fusion, so the
# # two generators can't drift apart (they build the same nodes/adjacency/records).
# # ---------------------------------------------------------------------------
# def _index_nodes(node_cids, chunks, into=None):
#     """Index a manifest's node chunks by global key. Returns (nodes_by_id, id_to_key):
#     `nodes_by_id` maps each node's Entity URI (or local-id fallback) to the node;
#     `id_to_key` maps its publisher-local id onto that same global key so edge
#     endpoints — still emitted as local ids — resolve onto identity.

#     Pass `into` to accumulate nodes across several sources (the view path); the
#     returned `id_to_key` is always fresh, since local ids only collide across
#     publishers and must be resolved per-source."""
#     nodes_by_id = into if into is not None else {}
#     id_to_key: dict = {}
#     for ncid in node_cids:
#         for node in (chunks.get(ncid) or []):
#             key = _node_key(node)
#             nodes_by_id[key] = node
#             if node.get("id") is not None:
#                 id_to_key[node["id"]] = key
#     return nodes_by_id, id_to_key


# def _build_adj(pairs):
#     """Undirected adjacency map from already-resolved (from_key, to_key) pairs."""
#     adj: dict = {}
#     for frm, to in pairs:
#         adj.setdefault(frm, []).append(to)
#         adj.setdefault(to, []).append(frm)
#     return adj


# def _project_records(profiles, nodes_by_id, adj, meta, defaults, prefer_key, log_prefix=""):
#     """Project every root node into a record, once per profile. `prefer_key` is the
#     node field used as the join id (`id` for a bundle, `entityUri` for a fused view).
#     Shared by both generators so their projection logic can't diverge."""
#     records = []
#     for prof in profiles:
#         rt = prof.get("root_type")
#         n_roots = 0
#         for node in nodes_by_id.values():
#             if node.get("type") != rt:
#                 continue
#             fields = _project(node, nodes_by_id, adj, prof, defaults)
#             records.append({
#                 "track_id":    _track_id(fields, prefer=node.get(prefer_key)),
#                 "entity_type": rt,
#                 "fields":      fields,
#                 "meta":        meta,
#             })
#             n_roots += 1
#         if n_roots == 0:
#             print(f"{log_prefix}profile {prof['name']!r}: no {rt!r} root nodes.")
#     return records


# # ---------------------------------------------------------------------------
# # OWNER:NAMESPACE PROJECTION — the read side of the new data model.
# #
# # `fangorn read <ns> --owner <addr>` hands back a namespace's graph as
# # {vertices:[{cid,schemaId,payload}], edges:[{sourceCid,relation,targetCid}]},
# # already content-addressed. This adapts that shape onto the SAME projection
# # core the bundle walk uses (`_build_adj` + `_project_records`), so there is one
# # graph-walk implementation, not two:
# #
# #   • each vertex CID is a globally unique, stable id — used directly as both the
# #     join key (nodes_by_id / adjacency) and the record's track_id (prefer_key
# #     "id"), so no field-sniffing heuristic is needed to derive one.
# #   • a vertex's `schemaId` is its `type` (what a profile's `root_type` matches).
# #   • edges are undirected for the walk, exactly as the bundle path treats them.
# # ---------------------------------------------------------------------------
# def project_source(owner: str, namespace: str, contents: dict,
#                    profiles: list[dict], args) -> list[dict]:
#     """Project one namespace's `{vertices, edges}` (from `fangorn read`) into
#     root-profile documents, reusing the shared projection core."""
#     nodes_by_id = {
#         v["cid"]: {"id": v["cid"], "type": v["schemaId"], "fields": v["payload"]}
#         for v in contents.get("vertices", [])
#     }
#     adj = _build_adj(
#         (e["sourceCid"], e["targetCid"]) for e in contents.get("edges", []))
#     defaults = {"max_depth": args.max_depth,
#                 "label_cap": args.label_cap, "node_cap": args.node_cap}
#     meta = {"owner": owner, "namespace": namespace}
#     return _project_records(profiles, nodes_by_id, adj, meta, defaults,
#                             prefer_key="id", log_prefix=f"[{owner}:{namespace}] ")

"""Graph projection: root profiles + the walk that turns a typed graph into documents.

A *profile* projects one graph from a chosen root type into a distinct document,
walking up to `max_depth` hops and folding included neighbor types into grouped,
deduped label lists. The shared join helpers at the bottom (`_index_nodes`,
`_build_adj`, `_project_records`) are used by BOTH the bundle walk and the view
fusion, so the two generators build nodes/adjacency/records identically.
"""
import json
import os

from quickbeam.ingest.identity import _track_id

def load_profiles(args, discovered_tags: set[str] | None = None) -> list[dict]:
    """Resolve --root-profile names (or auto-derive from what's actually in the
    source) into a list of fully-specified profile dicts. Each carries a `name`
    and `root_type` (a vertex tag / schemaId).

    This uses a zero-config default: it auto-derives one profile per distinct 
    vertex tag present in the source (`discovered_tags`), meaning the caller 
    can point quickbeam at a namespace with zero schema knowledge. 
    
    If specific tuning is needed (e.g. to exclude noisy high-degree neighbors 
    from a specific root), pass a JSON map via --profiles-file.
    """
    registry = {}
    if getattr(args, "profiles_file", None) and os.path.exists(args.profiles_file):
        with open(args.profiles_file) as f:
            for name, prof in (json.load(f) or {}).items():
                registry[name.lower()] = prof

    # If the user explicitly requested certain profiles
    if getattr(args, "root_profile", None):
        profiles = []
        for raw in args.root_profile:
            key = raw.strip().lower()
            prof = {"name": key, **registry[key]} if key in registry else \
                   {"name": key, "root_type": raw.strip()}
            prof.setdefault("max_depth", args.max_depth)
            prof.setdefault("include", None)
            profiles.append(prof)
        return profiles

    # Zero-config default: one profile per distinct vertex tag actually present
    # in this source, folding in whatever's within one hop (no curated `include`
    # filter — we don't know the shape of arbitrary data ahead of time).
    tags = sorted(discovered_tags or [])
    return [{"name": t.lower(), "root_type": t, "max_depth": args.max_depth, "include": None}
            for t in tags]


# Back-compat alias — callers pre-migration imported `_load_profiles`.
_load_profiles = load_profiles


def _node_key(node: dict) -> str:
    """Global join key for a node: its Entity URI when present else the raw local id. 
    Keying the adjacency and projections on this resolves edges on the globally-unique identity rather than a
    publisher-local id for cross-publisher linking."""
    return node.get("entityUri") or node.get("id")


def _node_label(node: dict) -> str:
    """Human label for a node — title / name / label, whichever the node carries."""
    f = node.get("fields", {}) or {}
    for k in ("title", "name", "label"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _node_content(node: dict, extra_keys=()) -> str:
    """Folded value for a neighbour node. Prefer a free-form *content* field (a
    Review `body`, an event/summary `description`) so that text becomes searchable
    when the node is folded into a root document — without this a Review folds in
    only its "<author> on <business>" title and the body ("best tacos in town") is
    silently dropped. Content-less nodes (Category, Locality, Reviewer) have none
    of these fields and fall back to their title label, so they don't bloat the doc.

    `extra_keys` (a profile's `content_fields`) are consulted first."""
    f = node.get("fields", {}) or {}
    for k in (*(extra_keys or ()), "body", "summary", "description"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return _node_label(node)

# normalize group keys
def _group_key(type_name: str) -> str:
    """Node type → camelCase plural field name. Artist→artists, Work→works,
    Place→places, ReleaseGroup→releaseGroups."""
    t = (type_name[:1].lower() + type_name[1:]) if type_name else type_name
    if t.endswith("y"):
        return t[:-1] + "ies"
    if t.endswith(("s", "x", "z", "ch", "sh")):
        return t + "es"
    return t + "s"

# walk the graph and rebuild the bundles
def _walk_graph(root_id, adj, max_depth, node_cap):
    """BFS from root over an (undirected) adjacency map, returning [(node_id, depth)]
    for every reachable node within `max_depth` (excluding the root). Bounded by
    `node_cap` so a high-degree hub can't blow up a single projection."""
    from collections import deque
    visited = {root_id}
    queue = deque([(root_id, 0)])
    collected = []
    while queue:
        nid, d = queue.popleft()
        if d >= max_depth:
            continue
        for nb in adj.get(nid, ()):  # neighbors
            if nb in visited:
                continue
            visited.add(nb)
            collected.append((nb, d + 1))
            if len(collected) >= node_cap:
                return collected
            queue.append((nb, d + 1))
    return collected


def _project(root, nodes_by_id, adj, profile, defaults):
    """Project a root node into a profile document: walk the graph and fold the
    included neighbors into grouped, deduped, capped label lists."""
    rt = profile.get("root_type") or root.get("type")
    fields = dict(root.get("fields", {}))

    depth = int(profile.get("max_depth", defaults["max_depth"]))
    label_cap = int(profile.get("label_cap", defaults["label_cap"]))
    node_cap = int(profile.get("node_cap", defaults["node_cap"]))
    include = profile.get("include")
    include_set = set(include) if include else None

    groups: dict = {}
    content_fields = profile.get("content_fields") or ()
    for nid, _depth in _walk_graph(_node_key(root), adj, depth, node_cap):
        nb = nodes_by_id.get(nid)
        if not nb:
            continue
        t = nb.get("type")
        if include_set is not None and t not in include_set:
            continue
        value = _node_content(nb, content_fields)
        if value:
            groups.setdefault(_group_key(t), []).append(value)

    for k, vals in groups.items():
        fields[k] = list(dict.fromkeys(vals))[:label_cap]  # dedupe (order-preserving) + cap
    fields["entityType"] = rt
    return fields

# ---------------------------------------------------------------------------
# SHARED JOIN HELPERS — used by both the bundle walk and the view fusion, so the
# two generators can't drift apart (they build the same nodes/adjacency/records).
# ---------------------------------------------------------------------------
def _index_nodes(node_cids, chunks, into=None):
    """Index a manifest's node chunks by global key. Returns (nodes_by_id, id_to_key):
    `nodes_by_id` maps each node's Entity URI (or local-id fallback) to the node;
    `id_to_key` maps its publisher-local id onto that same global key so edge
    endpoints — still emitted as local ids — resolve onto identity."""
    nodes_by_id = into if into is not None else {}
    id_to_key: dict = {}
    for ncid in node_cids:
        for node in (chunks.get(ncid) or []):
            key = _node_key(node)
            nodes_by_id[key] = node
            if node.get("id") is not None:
                id_to_key[node["id"]] = key
    return nodes_by_id, id_to_key


def _build_adj(pairs):
    """Undirected adjacency map from already-resolved (from_key, to_key) pairs."""
    adj: dict = {}
    for frm, to in pairs:
        adj.setdefault(frm, []).append(to)
        adj.setdefault(to, []).append(frm)
    return adj


def _project_records(profiles, nodes_by_id, adj, meta, defaults, prefer_key,
                     log_prefix="", cid_key=None):
    """Project every root node into a record, once per profile. `prefer_key` is the
    node field used as the join id (`id` for a bundle, `entityUri` for a fused view).
    `cid_key`, when set, names the node field holding its on-chain content id (CID);
    it is threaded into each record's `meta.sourceCid` so served results carry first-class,
    verifiable provenance (an empty `source_cid` is what made 'verifiable' aspirational).
    Shared by both generators so their projection logic can't diverge."""
    records = []
    for prof in profiles:
        rt = prof.get("root_type")
        n_roots = 0
        for node in nodes_by_id.values():
            if node.get("type") != rt:
                continue
            fields = _project(node, nodes_by_id, adj, prof, defaults)
            cid = node.get(cid_key) if cid_key else None
            records.append({
                "track_id":    _track_id(fields, prefer=node.get(prefer_key)),
                "entity_type": rt,
                "fields":      fields,
                "meta":        {**meta, "sourceCid": cid} if cid else meta,
            })
            n_roots += 1
        if n_roots == 0:
            print(f"{log_prefix}profile {prof['name']!r}: no {rt!r} root nodes.")
    return records


# ---------------------------------------------------------------------------
# OWNER:NAMESPACE PROJECTION — the read side of the new data model.
# ---------------------------------------------------------------------------
def project_source(owner: str, namespace: str, contents: dict,
                   profiles: list[dict], args) -> list[dict]:
    """Project one namespace's `{vertices, edges}` (from `fangorn read`) into
    root-profile documents, reusing the shared projection core."""
    nodes_by_id = {
        v["cid"]: {"id": v["cid"], "type": v["schemaId"], "fields": v["payload"]}
        for v in contents.get("vertices", [])
    }
    adj = _build_adj(
        (e["sourceCid"], e["targetCid"]) for e in contents.get("edges", []))
    defaults = {"max_depth": getattr(args, "max_depth", 2),
                "label_cap": getattr(args, "label_cap", 50), 
                "node_cap": getattr(args, "node_cap", 1000)}
    meta = {"owner": owner, "namespace": namespace}
    # `id` on each projected node is its on-chain vertex CID (see the nodes_by_id
    # comprehension above), so thread it in as the record's verifiable source CID.
    return _project_records(profiles, nodes_by_id, adj, meta, defaults,
                            prefer_key="id", log_prefix=f"[{owner}:{namespace}] ",
                            cid_key="id")