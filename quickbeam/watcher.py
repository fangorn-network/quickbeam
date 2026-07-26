"""
quickbeam watch — live embedding daemon.

Subscribes to one or more owner+namespace sources via `fangorn subscribe` (a
push-based light-client stream — no indexer, no busy polling) and embeds
new/changed vertices into Qdrant as commits land. Each streamed line is a
self-contained on-chain diff (added/removed vertices + edges); the watcher
applies it to an in-memory snapshot of the namespace and re-projects, so
neighbor-folding profiles still see the whole graph. On startup each source is
seeded once with `fangorn read` so the existing corpus is embedded before the
stream goes live — a commit that lands during seeding is buffered by the
subscription and applied afterwards idempotently (vertices are content-addressed
and the checkpoint dedupes), so nothing is lost.

Examples
--------
  # Watch one source:
  quickbeam watch --source 0x147c24c5...:robinhood \\
      --root-profile asset --root-profile transfer

  # Watch several sources into the same collection (independently — no
  # cross-source identity fusion):
  quickbeam watch --source 0x147c...:robinhood --source 0x9a38...:music
"""

import argparse
import asyncio
import json
import os
import sys

from quickbeam.ingest.checkpoint import (
    _load_checkpoint, _save_checkpoint, _save_role_map)
from quickbeam.ingest.embed import (
    MODEL_DIM_MAP, _init_embed_engine, _embed_and_upload, ensure_indexes,
    _structured_types)
from quickbeam.ingest.graph.projection import load_profiles, project_source
from quickbeam.ingest.identity import _str_to_uuid
from quickbeam.ingest.sources.fangorn import (
    parse_sources, subscribe_cmd, read_ndjson_cmd)
from qdrant_client import QdrantClient
from qdrant_client import models
from quickbeam.roles import infer_roles, role_map_applies


# `fangorn subscribe`/`read` emit one whole NamespaceChange (or the full seed) as a
# SINGLE line, which for a real commit is many vertices — routinely well past asyncio's
# default 64 KiB StreamReader buffer, so `async for line in proc.stdout` raises
# "Separator is not found, and chunk exceed the limit" and kills the stream. Give the
# subprocess pipes a generous per-line ceiling (the buffer only grows to the actual line
# size; this is just the cap before it errors).
_STREAM_LIMIT = 256 * 1024 * 1024  # 256 MiB


# ---------------------------------------------------------------------------
# ARG PARSING
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="Quickbeam Watch — live embedding daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument("--source", action="append", dest="sources", default=[],
                   help="OWNER:NAMESPACE to watch, repeatable.")
    p.add_argument("--fangorn-bin", default="fangorn",
                   help="How to invoke the fangorn CLI (may be a full command).")

    # Projection profiles — how graph roots become documents. Mirrors `build`.
    p.add_argument("--root-profile", action="append", default=[],
                   help="Named projection(s) to emit, repeatable. If omitted, one "
                        "profile per distinct vertex tag actually present is "
                        "auto-derived each cycle.")
    p.add_argument("--profiles-file", default=None,
                   help="Optional JSON file of custom/override root profiles.")
    p.add_argument("--max-depth", type=int, default=1, help="Graph-walk depth per profile.")
    p.add_argument("--label-cap", type=int, default=50, help="Max folded labels per group.")
    p.add_argument("--node-cap", type=int, default=2000, help="Max nodes visited per root.")

    p.add_argument("--poll-interval", type=int, default=60,
                   help="Seconds to wait before re-subscribing if the stream drops "
                        "(default: 60). The watch itself is push-based — this is a "
                        "reconnect backoff, not a poll timer.")
    p.add_argument("--seed-timeout", type=int, default=180,
                   help="Max seconds to wait for the startup `fangorn read` seed of the "
                        "full namespace (default: 180). If the read exceeds this (a large "
                        "namespace can be a slow light-client read), the watcher gives up "
                        "on the seed and goes live on the subscribe stream instead of "
                        "freezing; it retries the seed on the next reconnect until it "
                        "succeeds once.")

    # ── Live CDN delivery ─────────────────────────────────────────────────────
    # When set, after any change that embeds new records the watcher writes them as a
    # delta shard into the baked CDN domain (see cdn.append_domain). This closes the
    # loop: on-chain publish → embed → deliver, shipping only the delta (no re-bake).
    g2 = p.add_argument_group("live CDN delivery (optional)")
    g2.add_argument("--cdn-dir", default=None,
                    help="Baked CDN directory. Enables live delta delivery when set.")
    g2.add_argument("--cdn-domain", default=None,
                    help="Domain to append new records to (must already be baked).")
    g2.add_argument("--cdn-config", default="domains.json",
                    help="Domain config used to resolve the append scan filter.")

    # ── Shared with build ─────────────────────────────────────────────────────
    p.add_argument("--qdrant-host", default="localhost")
    p.add_argument("--qdrant-port", type=int, default=6333)
    p.add_argument("--qdrant-grpc-port", type=int, default=6334)
    p.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    p.add_argument("--collection", default="fangorn")
    p.add_argument("--searchable-fields", default="auto")
    p.add_argument("--structured-types", default="",
                   help="Comma-separated entityType names to INDEX+SERVE but NOT embed "
                        "(e.g. PriceBar). Structured rows queried only via where{...}/"
                        "aggregate skip the embed model and ride a constant placeholder "
                        "vector — see embed._embed_and_upload.")
    p.add_argument("--embedding-model", default="nomic-ai/nomic-embed-text-v1.5")
    p.add_argument("--dim", type=int, default=256)
    p.add_argument("--embed-batch", type=int, default=16)
    p.add_argument("--role-map-file", default="./db/role_map.json")

    return p.parse_args()


# ---------------------------------------------------------------------------
# INGEST — project a full namespace snapshot, diff, embed
# ---------------------------------------------------------------------------
async def _ingest_contents(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                           checkpoint, owner, namespace, contents,
                           edges_sink=None, tombstones_sink=None):
    """Project one source's full {vertices, edges} snapshot into documents, diff the
    root set against the last projected snapshot, tombstone roots that dropped out,
    and embed roots that are new. Shared by the initial seed read and every streamed
    change (the change is applied to an in-memory snapshot first, then re-projected
    here — projection is local CPU, so re-running it per change is cheap). Returns the
    number of new records embedded."""
    key = f"{owner}:{namespace}"
    src_ck = checkpoint.setdefault("sources", {}).setdefault(key, {})

    cid_to_tag = {v["cid"]: v["schemaId"] for v in contents.get("vertices", [])}
    discovered_tags = set(cid_to_tag.values())
    profiles = load_profiles(args, discovered_tags)
    records = project_source(owner, namespace, contents, profiles, args)
    by_track_id = {r["track_id"]: r for r in records}

    prev_ids = set(src_ck.get("vertex_cids", []))
    curr_ids = set(by_track_id.keys())
    removed = prev_ids - curr_ids
    new_records = [by_track_id[t] for t in curr_ids if t not in prev_ids]

    if removed:
        qdrant.delete(collection_name=args.collection,
                      points_selector=models.PointIdsList(points=[_str_to_uuid(t) for t in removed]),
                      wait=True)
        print(f"[Watcher] {key}: tombstoned {len(removed)} point(s)")
        if tombstones_sink is not None:
            tombstones_sink.extend(removed)

    if edges_sink is not None:
        for e in contents.get("edges", []):
            edges_sink.append({
                "rel": e["relation"], "from": e["sourceCid"], "to": e["targetCid"],
                "fromType": cid_to_tag.get(e["sourceCid"]), "toType": cid_to_tag.get(e["targetCid"]),
            })

    src_ck["vertex_cids"] = list(curr_ids)
    _save_checkpoint(checkpoint, args.checkpoint_file)

    if not new_records:
        return 0

    # Infer the role map from the first real batch if none is loaded yet, OR if
    # the loaded map doesn't apply to these records — a stale ./db/role_map.json
    # from a different corpus would otherwise make every record embed the same
    # empty "Title: . Tags:" text, collapsing all vectors to one point.
    rec_fields = [r["fields"] for r in new_records]
    if not role_map_ref[0] or not role_map_applies(role_map_ref[0], rec_fields):
        if role_map_ref[0]:
            print("[Watcher] loaded role map does not match these records "
                  "(stale/foreign) — re-inferring")
        role_map_ref[0] = infer_roles(rec_fields)
        _save_role_map(role_map_ref[0], args.role_map_file)
        print(f"[Watcher] Role map inferred and saved to {args.role_map_file}")

    print(f"[Watcher] {key}: {len(new_records)} new record(s)")
    await _embed_and_upload(args, qdrant, embed_engine, new_records, role_map_ref[0],
                            dim, truncate, checkpoint)
    _save_checkpoint(checkpoint, args.checkpoint_file)
    return len(new_records)


# ---------------------------------------------------------------------------
# LIVE CDN DELIVERY — ship a change's delta into the baked domain
# ---------------------------------------------------------------------------
def _deliver_cdn(args, qdrant, total_new, edges, tombstones):
    """Mirror an ingested change into the delivered CDN domain: append the new points
    as a delta shard, ride removals on the manifest's tombstone list, and merge new
    edges into the served graph. Each step is isolated — a delivery failure never
    kills the watch stream."""
    if not (args.cdn_dir and args.cdn_domain):
        return

    if total_new:
        try:
            from quickbeam.cdn import append_domain
            append_domain(qdrant, args.collection, args.cdn_dir,
                          args.cdn_domain, config_path=args.cdn_config)
        except Exception as e:  # noqa: BLE001
            print(f"[Watcher] CDN append error: {e}", file=sys.stderr)

    # Shards are immutable, so removals ride the manifest's tombstones list
    # (clients drop those rows at load; dead edges are pruned). Only tombstone
    # points that are actually gone from the collection.
    if tombstones:
        try:
            from quickbeam.cdn import append_tombstones
            by_uuid = {_str_to_uuid(t): t for t in set(tombstones)}
            found = qdrant.retrieve(args.collection, ids=list(by_uuid),
                                    with_payload=False, with_vectors=False)
            alive = {str(pt.id) for pt in found}
            dead = [t for u, t in by_uuid.items() if u not in alive]
            if dead:
                append_tombstones(args.cdn_dir, args.cdn_domain, dead)
        except Exception as e:  # noqa: BLE001
            print(f"[Watcher] CDN tombstone error: {e}", file=sys.stderr)

    if edges:
        try:
            from quickbeam.cdn import append_edges
            added = append_edges(args.cdn_dir, args.cdn_domain, edges)
            if added:
                print(f"[Watcher] CDN edges: +{added['added']} new "
                      f"({added['count']} total; relations={added['relations']})")
        except Exception as e:  # noqa: BLE001
            print(f"[Watcher] CDN edges error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# SINGLE SOURCE STREAM
# ---------------------------------------------------------------------------
def _edge_key(e: dict) -> tuple:
    return (e["sourceCid"], e["relation"], e["targetCid"])


async def _drain_stderr(key: str, stream):
    """Forward the subscribe subprocess's stderr (status lines) to ours so its pipe
    never fills, prefixed for provenance."""
    async for raw in stream:
        line = raw.decode(errors="replace").rstrip()
        if line:
            print(f"[fangorn subscribe {key}] {line}", file=sys.stderr)


async def _iter_read_ndjson(fangorn_bin: str, owner: str, namespace: str,
                            idle_timeout: float):
    """Async generator over `fangorn read <ns> --owner <owner> --ndjson`, yielding one
    parsed `{kind:head|vertex|edge}` dict per stdout line.

    Why streaming (not the old `communicate()` + `json.loads`): a full-namespace read of
    a large corpus (e.g. ~780k price bars) is materialized+JSON.stringify'd on the fangorn
    side and buffered+parsed on this side — several full copies across two processes, which
    OOMs the machine before any embedding. `--ndjson` emits one small object per line so the
    reader can embed and free each vertex without ever holding the whole corpus.

    `idle_timeout` bounds the gap BETWEEN lines (not the total read, which is legitimately
    minutes for a big namespace): a hung/silent read still degrades to 'skip the seed, go
    live, retry next reconnect' instead of freezing, exactly as the old total timeout did."""
    proc = await asyncio.create_subprocess_exec(
        *read_ndjson_cmd(fangorn_bin, owner, namespace),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_STREAM_LIMIT,
    )
    stderr_task = asyncio.create_task(_drain_stderr(f"read {owner}:{namespace}", proc.stderr))
    try:
        assert proc.stdout is not None
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"fangorn read produced no output for {idle_timeout:.0f}s")
            if not raw:
                break  # EOF
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            yield json.loads(line)
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError(f"fangorn read exited rc={rc}")
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        stderr_task.cancel()


async def _ingest_structured(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                             checkpoint, owner, namespace, batch):
    """Project + upsert a batch of STRUCTURED (edgeless, non-embedded) vertices, then
    return the count. Structured types (--structured-types, e.g. PriceBar) are edgeless
    rows served only via where{}/aggregate — never graph-walked (see the projection
    folding walk) — so they bypass the resident snapshot and the tombstone diff entirely
    and can be projected+indexed in bounded batches and dropped, instead of holding all
    ~780k at once. They ride a constant placeholder vector (the embed MODEL is skipped —
    see _embed_and_upload), so the role map is irrelevant to them.

    CDN delivery is NOT done here: append_domain re-scrolls the whole collection (with
    vectors) and loads every delivered id per call, so delivering per batch is O(n^2) and
    memory-heavy at seed scale. The seed instead bakes once (memory-bounded) after the full
    stream; live changes deliver their small delta via the normal _deliver_cdn path."""
    if not batch:
        return 0
    contents = {"vertices": batch, "edges": []}
    profiles = load_profiles(args, {v["schemaId"] for v in batch})
    records = project_source(owner, namespace, contents, profiles, args)
    await _embed_and_upload(args, qdrant, embed_engine, records, role_map_ref[0] or {},
                            dim, truncate, checkpoint)
    _save_checkpoint(checkpoint, args.checkpoint_file)
    return len(records)


def _bake_cdn_seed(args, qdrant, edges):
    """One-shot, memory-bounded CDN delivery for a completed seed: bake the whole domain
    from the collection (bake_domain streams to rolling shards — bounded memory, unlike
    the per-delta append which scrolls+buffers the whole collection), then append the
    (small) embedded-graph edges. Used only after a clean seed; per-change deltas keep
    using _deliver_cdn."""
    if not (args.cdn_dir and args.cdn_domain):
        return
    try:
        from quickbeam.cdn import bake_domain
        entry = bake_domain(qdrant, args.collection, args.cdn_dir, args.cdn_domain,
                            config_path=args.cdn_config, model=args.embedding_model)
        print(f"[Watcher] CDN seed bake: {entry['count']:,} point(s) into "
              f"{args.cdn_dir}/{args.cdn_domain}")
    except Exception as e:  # noqa: BLE001
        print(f"[Watcher] CDN seed bake error: {e}", file=sys.stderr)
    if edges:
        try:
            from quickbeam.cdn import append_edges
            added = append_edges(args.cdn_dir, args.cdn_domain, edges)
            if added:
                print(f"[Watcher] CDN edges: +{added['added']} new ({added['count']} total)")
        except Exception as e:  # noqa: BLE001
            print(f"[Watcher] CDN seed edges error: {e}", file=sys.stderr)


async def _stream_source_once(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                              checkpoint, owner, namespace, snapshot):
    """Seed one owner:namespace source (once), then consume its `fangorn subscribe`
    stream until it ends. Returns normally when the stream closes (caller resubscribes).

    `snapshot` is the in-memory namespace state ({vertices, edges, seeded}), owned by the
    caller so it PERSISTS across reconnects — we do the expensive full `fangorn read`
    seed only until it succeeds once, then reconnects reuse the snapshot and rely on the
    subscribe cursor replaying any commits missed while down. Re-reading the whole
    namespace on every reconnect is what made a slow read freeze the watcher in a loop."""
    key = f"{owner}:{namespace}"

    # Start the subscription FIRST so any commit that lands while we seed buffers in
    # the pipe. Applying such a buffered change after the seed is idempotent: vertices
    # are content-addressed (re-adding a cid the seed already has is a no-op) and the
    # checkpoint dedupes embeds — so the seed↔live gap loses nothing.
    proc = await asyncio.create_subprocess_exec(
        *subscribe_cmd(args.fangorn_bin, owner, namespace),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_STREAM_LIMIT,
    )
    print(f"[Watcher] {key}: subscribed (pid {proc.pid})")
    stderr_task = asyncio.create_task(_drain_stderr(key, proc.stderr))

    try:
        vertices_by_cid = snapshot["vertices"]
        edges_by_key = snapshot["edges"]

        structured = _structured_types(args)

        # Seed current on-chain state ONCE so the existing corpus is embedded before we go
        # live. STREAMED (see _iter_read_ndjson): structured/edgeless rows (e.g. 780k price
        # bars) are projected+indexed in bounded batches and dropped — never held all at
        # once — so a large namespace no longer OOMs the machine on the seed fetch. Only the
        # (small) embedded graph (Assets/Transfers + their edges) stays resident for folding.
        # A slow/hung read still degrades to 'skip seed, go live, retry next reconnect'; a
        # mid-stream failure leaves seeded=False so the whole stream re-runs (idempotent).
        if not snapshot["seeded"]:
            vertices_by_cid.clear()
            edges_by_key.clear()
            struct_batch: list = []
            STRUCT_BATCH = 20000
            saw_vertex = False
            head_val = "__unset__"
            try:
                async for item in _iter_read_ndjson(
                        args.fangorn_bin, owner, namespace, args.seed_timeout):
                    kind = item.get("kind")
                    if kind == "head":
                        head_val = item.get("head")
                    elif kind == "vertex":
                        saw_vertex = True
                        v = {"cid": item["cid"], "schemaId": item["schemaId"],
                             "payload": item["payload"]}
                        if item["schemaId"] in structured:
                            struct_batch.append(v)
                            if len(struct_batch) >= STRUCT_BATCH:
                                await _ingest_structured(
                                    args, qdrant, embed_engine, role_map_ref, dim, truncate,
                                    checkpoint, owner, namespace, struct_batch)
                                print(f"[Watcher] {key}: seeded {len(struct_batch)} "
                                      f"structured row(s) (running)")
                                struct_batch = []
                        else:
                            vertices_by_cid[item["cid"]] = v
                    elif kind == "edge":
                        e = {"sourceCid": item["sourceCid"], "relation": item["relation"],
                             "targetCid": item["targetCid"]}
                        edges_by_key[_edge_key(e)] = e

                # Reject a degenerate read: null head + zero vertices means onChainTip came
                # back null (flaky/unsettled), NOT an authoritatively-empty namespace —
                # seeding on it would mark the source seeded on nothing. A genuinely empty
                # namespace still returns a non-null head, so this only rejects the broken case.
                if head_val is None and not saw_vertex:
                    raise RuntimeError("read returned null head + empty namespace "
                                       "(unsettled head or flaky RPC read) — not seeding")

                # Flush the trailing structured batch, then ingest the resident embedded
                # graph (folding needs the whole small graph — now fully in memory).
                if struct_batch:
                    await _ingest_structured(
                        args, qdrant, embed_engine, role_map_ref, dim, truncate,
                        checkpoint, owner, namespace, struct_batch)
                    struct_batch = []

                seed_edges: list = []
                n = await _ingest_contents(
                    args, qdrant, embed_engine, role_map_ref, dim, truncate,
                    checkpoint, owner, namespace,
                    {"vertices": list(vertices_by_cid.values()),
                     "edges": list(edges_by_key.values())},
                    edges_sink=seed_edges,
                )
                snapshot["seeded"] = True
                print(f"[Watcher] {key}: seeded — {n or 'no'} embedded record(s) + "
                      f"structured rows indexed")
                _bake_cdn_seed(args, qdrant, seed_edges)
            except Exception as e:  # noqa: BLE001
                print(f"[Watcher] {key}: seed skipped ({e}); going live on the stream, "
                      f"will retry seed on reconnect", file=sys.stderr)
                # Partial uploads (if any) are idempotent; seeded stays False so the next
                # reconnect re-streams the whole namespace and re-bakes cleanly.
                vertices_by_cid.clear()
                edges_by_key.clear()
        else:
            print(f"[Watcher] {key}: reusing snapshot "
                  f"({len(vertices_by_cid)} vertices) — resuming stream")

        # Consume the live change stream. Each line is one self-contained NamespaceChange.
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                change = json.loads(line)
            except json.JSONDecodeError:
                print(f"[Watcher] {key}: unparseable change line: {line[:200]}", file=sys.stderr)
                continue

            removed_vertex = change.get("removedVertexCids", [])
            added_vertices = change.get("addedVertices", [])
            # Structured/edgeless adds bypass the resident snapshot + tombstone diff and
            # are indexed directly (same as the seed). Keeping them OUT of vertices_by_cid
            # is what makes the first live change safe: were they tracked, the diff below
            # would see them as "removed" and mass-tombstone the whole collection.
            # ponytail: structured-type REMOVALS aren't tombstoned — bars are append-only
            # historical/live rows, never deleted. Add a direct qdrant.delete on removed
            # structured cids here if a structured type ever starts deleting.
            struct_added = [v for v in added_vertices if v["schemaId"] in structured]
            embed_added = [v for v in added_vertices if v["schemaId"] not in structured]

            n_struct = 0
            if struct_added:
                n_struct = await _ingest_structured(
                    args, qdrant, embed_engine, role_map_ref, dim, truncate,
                    checkpoint, owner, namespace, struct_added)

            # Apply the embedded diff to the in-memory snapshot: removals first, then adds
            # (an add re-pointing a cid must win over a same-line removal of the old one).
            for cid in removed_vertex:
                vertices_by_cid.pop(cid, None)          # structured cids aren't here → no-op
            for e in change.get("removedEdges", []):
                edges_by_key.pop(_edge_key(e), None)
            for v in embed_added:
                vertices_by_cid[v["cid"]] = v
            for e in change.get("addedEdges", []):
                edges_by_key[_edge_key(e)] = e

            contents = {"vertices": list(vertices_by_cid.values()),
                        "edges": list(edges_by_key.values())}

            src_ck = checkpoint.setdefault("sources", {}).setdefault(key, {})
            src_ck["head"] = change.get("newRoot")
            src_ck["block"] = change.get("blockNumber")

            print(f"[Watcher] {key}: change @ block {change.get('blockNumber')} "
                  f"(+{len(added_vertices)} / -{len(removed_vertex)} vertices) "
                  f"→ {change.get('commitCid')}")

            change_edges: list = []
            change_tombstones: list = []
            n = await _ingest_contents(
                args, qdrant, embed_engine, role_map_ref, dim, truncate,
                checkpoint, owner, namespace, contents,
                edges_sink=change_edges, tombstones_sink=change_tombstones,
            )
            status = (f"{n} embedded + {n_struct} structured record(s)"
                      if (n or n_struct) else "no new records for the active profiles")
            print(f"[Watcher] {key}: change applied — {status}")
            # One delivery covers both: append_domain scrolls the collection and picks up
            # every un-delivered point (embedded AND the just-indexed structured rows).
            _deliver_cdn(args, qdrant, n + n_struct, change_edges, change_tombstones)

        rc = await proc.wait()
        print(f"[Watcher] {key}: subscribe stream ended (rc={rc})", file=sys.stderr)
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        stderr_task.cancel()


async def _stream_source(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                         checkpoint, owner, namespace):
    """Supervise one source forever: (re)subscribe, and if the stream drops, back off
    --poll-interval seconds and reconnect. `fangorn subscribe` persists its own resume
    cursor, so a reconnect replays commits missed while we were down."""
    key = f"{owner}:{namespace}"
    # Persist the in-memory namespace snapshot across reconnects so the expensive full
    # seed read runs only until it succeeds once; later reconnects reuse it and lean on
    # the subscribe cursor to replay anything missed while down.
    snapshot = {"seeded": False, "vertices": {}, "edges": {}}
    while True:
        try:
            await _stream_source_once(
                args, qdrant, embed_engine, role_map_ref, dim, truncate,
                checkpoint, owner, namespace, snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a bad source must not kill its peers
            print(f"[Watcher] {key}: stream error: {e}", file=sys.stderr)
        print(f"[Watcher] {key}: reconnecting in {args.poll_interval}s...")
        await asyncio.sleep(args.poll_interval)


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
async def main():
    args = parse_args()

    sources = parse_sources(args.sources)
    if not sources:
        sys.exit("[Watcher] pass at least one --source OWNER:NAMESPACE.")

    model_dim = MODEL_DIM_MAP.get(args.embedding_model, 768)
    dim       = min(args.dim, model_dim)
    truncate  = dim < model_dim

    print(f"[Watcher] Starting — sources: {', '.join(f'{o}:{n}' for o, n in sources)}")
    print(f"[Watcher] mode: push (fangorn subscribe); reconnect backoff {args.poll_interval}s")

    qdrant = QdrantClient(
        host=args.qdrant_host, port=args.qdrant_port,
        grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=600
    )

    if not qdrant.collection_exists(args.collection):
        qdrant.create_collection(
            args.collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE, on_disk=True)
        )
    ensure_indexes(qdrant, args.collection)

    # Init embed engine once and keep it alive across changes.
    embed_engine = _init_embed_engine(args)
    print(f"[Watcher] model dim={model_dim}, output dim={dim} (truncate={truncate})")

    # Load existing role map; inferred on first real batch if absent.
    role_map_ref = [{}]
    if os.path.exists(args.role_map_file):
        with open(args.role_map_file) as f:
            role_map_ref[0] = json.load(f)

    checkpoint = _load_checkpoint(args.checkpoint_file)

    # Bootstrap live CDN delivery. The per-change delta path (append_domain) can only
    # EXTEND an already-baked domain, so if a delivery target is set but not yet baked,
    # bake it once now from whatever the collection already holds. This makes `cdn serve`
    # start immediately (no manual `cdn bake`) and gives append_domain the base manifest
    # it grows per change. Domains need no domains.json entry — a missing spec bakes all.
    if args.cdn_dir and args.cdn_domain:
        manifest_path = os.path.join(args.cdn_dir, args.cdn_domain, "manifest.json")
        # Re-bake when there's no manifest OR the existing one is EMPTY (count 0) while the
        # collection actually holds points. An empty manifest is never intentional: the
        # startup bake races AHEAD of the first seed embed, so a fresh run bakes 0, and a
        # later run then finds all cids already embedded (checkpoint says seeded) → no new
        # records → no append delta → and the append path only EXTENDS an existing manifest,
        # never seeds it. That wedges the served domain empty forever while Qdrant is full
        # (the exact failure this guards against). Comparing only against count==0 keeps the
        # incremental append path untouched for a normally-baked domain; use `cdn bake` for
        # a full manual refresh.
        baked_count = None
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path) as f:
                    baked_count = int(json.load(f).get("count", 0))
            except (OSError, ValueError, TypeError):
                baked_count = None            # unreadable manifest → treat as needs-bake
        try:
            collection_count = qdrant.count(collection_name=args.collection, exact=True).count
        except Exception:  # noqa: BLE001
            collection_count = 0
        needs_bake = baked_count is None or (baked_count == 0 and collection_count > 0)
        if not needs_bake:
            print(f"[Watcher] CDN domain {args.cdn_domain!r} already baked "
                  f"({baked_count:,} pt) — deltas append per change")
        else:
            why = ("not baked" if baked_count is None
                   else f"baked empty but collection holds {collection_count:,}")
            print(f"[Watcher] CDN domain {args.cdn_domain!r} {why} — baking snapshot...")
            try:
                from quickbeam.cdn import bake_domain
                entry = bake_domain(qdrant, args.collection, args.cdn_dir, args.cdn_domain,
                                    config_path=args.cdn_config, model=args.embedding_model)
                print(f"[Watcher] CDN bake: {entry['count']:,} point(s) into "
                      f"{args.cdn_dir}/{args.cdn_domain}")
            except Exception as e:  # noqa: BLE001
                print(f"[Watcher] CDN bake failed: {e}", file=sys.stderr)

    # One independent subscription per source, all live concurrently.
    await asyncio.gather(*(
        _stream_source(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                       checkpoint, owner, namespace)
        for owner, namespace in sources
    ))


if __name__ == "__main__":
    asyncio.run(main())
