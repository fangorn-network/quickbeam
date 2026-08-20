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

A namespace is an `app:publisher:subspace` triple. `--source` carries the last two;
the app comes from `--app` (or, unset, from the local fangorn client's config).

Examples
--------
  # Watch one source:
  quickbeam watch --source 0x147c24c5...:robinhood \\
      --root-profile asset --root-profile transfer

  # Watch several sources into the same collection (independently — no
  # cross-source identity fusion):
  quickbeam watch --source 0x147c...:robinhood --source 0x9a38...:music

  # Watch an entire app — every publisher, every subspace in it:
  quickbeam watch --app sond3r.test.1 --source '*:*'
"""

import argparse
import asyncio
import copy
import json
import os
import re
import shlex
import sys
import urllib.request

from quickbeam.ingest.checkpoint import (
    _load_checkpoint, _save_checkpoint, _save_role_map)
from quickbeam.ingest.embed import (
    MODEL_DIM_MAP, _init_embed_engine, _embed_and_upload, ensure_indexes)
from quickbeam.ingest.graph.projection import load_profiles, project_source
from quickbeam.ingest.identity import _str_to_uuid
from quickbeam.ingest.sources.fangorn import (
    app_env, parse_sources, subscribe_cmd)
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
                   help="OWNER:NAMESPACE to watch, repeatable. `*` on either side is a "
                        "wildcard that widens the subscription to the app level: "
                        "'0x..:*' (one publisher, every namespace), '*:docs' (that "
                        "namespace across publishers), '*:*' (the whole app).")
    p.add_argument("--sources-url", default=None, metavar="URL",
                   help="Poll this URL for the watch list instead of fixed --source "
                        "flags. Sources that appear start streaming and sources that "
                        "vanish are cancelled, with no restart. Accepts "
                        '["OWNER:NS", …], [{"owner":…,"namespace":…}, …], or either '
                        'wrapped in {"sources": …}.')
    p.add_argument("--sources-refresh", type=int, default=60,
                   help="Seconds between --sources-url polls (default: 60).")
    p.add_argument("--app", default=None,
                   help="App (global namespace) to watch — a name or a 32-byte app id. The "
                        "app is the first part of the app:publisher:subspace triple and is "
                        "NOT part of --source. Unset means whatever the local fangorn client "
                        "is configured with (`fangorn set-app`, ~/.fangorn/config.json).")
    p.add_argument("--from-block", type=int, default=None,
                   help="Replay each source's commits from this block before going live. "
                        "This is how an app-level (`*`) watch picks up namespaces published "
                        "before it started — a wildcard source has no single namespace to "
                        "seed with `fangorn read`. Catch-up is fetched in windows "
                        "(FANGORN_LOG_WINDOW blocks each), so pick a block near the first "
                        "publish, not genesis.")
    p.add_argument("--from-start", action="store_true",
                   help="Replay from genesis (ignored if --from-block is set). Only "
                        "practical on a private RPC with a large log window.")
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
    p.add_argument("--qdrant-url", default=None, metavar="URL",
                   help="Qdrant base URL, e.g. https://qdrant.example.com (overrides "
                        "--qdrant-host/port). Use for a remote/authenticated Qdrant.")
    p.add_argument("--qdrant-api-key", default=None,
                   help="Qdrant API key (QDRANT__SERVICE__API_KEY on the server).")
    p.add_argument("--qdrant-host", default="localhost")
    p.add_argument("--qdrant-port", type=int, default=6333)
    p.add_argument("--qdrant-grpc-port", type=int, default=6334)
    p.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    p.add_argument("--collection", default="fangorn")
    p.add_argument("--searchable-fields", default="auto")
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
    # Keyed on the whole app:publisher:subspace triple. Two apps can hold the same
    # publisher:subspace, and a shared key would make each one's snapshot diff read as
    # "every vertex removed" against the other's — tombstoning both corpora in turn.
    key = f"{args.app}:{owner}:{namespace}"
    src_ck = checkpoint.setdefault("sources", {}).setdefault(key, {})

    cid_to_tag = {v["cid"]: v["schemaId"] for v in contents.get("vertices", [])}
    discovered_tags = set(cid_to_tag.values())
    profiles = load_profiles(args, discovered_tags)
    records = project_source(args.app, owner, namespace, contents, profiles, args)
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

    if not new_records:
        # Nothing to upload, so recording the projected set now risks nothing — and this
        # is the path that has to persist the tombstones applied just above.
        src_ck["vertex_cids"] = list(curr_ids)
        _save_checkpoint(checkpoint, args.checkpoint_file)
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

    # ONLY after the upload lands. `vertex_cids` is what makes the NEXT diff treat these
    # records as already held, so persisting it before the upload turns any transient
    # Qdrant failure into permanent loss: the records are marked present, `new_records`
    # comes back empty forever, and the CDN domain bakes zero shards while the seed log
    # cheerfully reports "no new records". That is not hypothetical — a full disk did
    # exactly this in production. Point ids are deterministic (_str_to_uuid), so
    # re-running an upload that partly succeeded just re-upserts the same points.
    src_ck["vertex_cids"] = list(curr_ids)
    _save_checkpoint(checkpoint, args.checkpoint_file)
    return len(new_records)


# ---------------------------------------------------------------------------
# LIVE CDN DELIVERY — ship a change's delta into the baked domain
# ---------------------------------------------------------------------------
def _deliver_cdn(args, qdrant, total_new, edges, tombstones, owner=None, namespace=None,
                 app=None):
    """Mirror an ingested change into the delivered CDN domain: append the new points
    as a delta shard, ride removals on the manifest's tombstone list, and merge new
    edges into the served graph. Each step is isolated — a delivery failure never
    kills the watch stream."""
    if not (args.cdn_dir and args.cdn_domain):
        return

    if total_new:
        try:
            from quickbeam.cdn import append_domain
            # Scope the scan to this source. One collection can hold several watched
            # namespaces, and an unfiltered append would pull every other namespace's
            # points into this domain.
            append_domain(qdrant, args.collection, args.cdn_dir,
                          args.cdn_domain, config_path=args.cdn_config,
                          owners=[owner] if owner else None,
                          namespaces=[namespace] if namespace else None,
                          apps=[app] if app else None)
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


async def _seed_read_async(fangorn_bin: str, owner: str, namespace: str,
                           timeout: float, app: str | None = None) -> dict:
    """Async, timeout-bounded `fangorn read <ns> --owner <owner>` for the startup seed.

    CRITICAL: the seed MUST NOT use the synchronous `read_source` (subprocess.run) —
    that blocks the asyncio event loop for the entire read, and a full-namespace read
    is a slow O(namespace) light-client fetch (thousands of IPFS chunk gets) that can
    run for many minutes. Blocking the loop freezes stderr draining and the subscribe
    stream, so the whole watcher hangs on the seed and never goes live. Here the read is
    its own child process we can wait on with a hard timeout and kill if it overruns, so
    a slow/hung read degrades to 'skip the seed, go live' instead of freezing."""
    proc = await asyncio.create_subprocess_exec(
        *shlex.split(fangorn_bin), "read", namespace, "--owner", owner,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_STREAM_LIMIT, env=app_env(app),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        raise TimeoutError(f"fangorn read exceeded {timeout:.0f}s")
    if proc.returncode != 0:
        raise RuntimeError(f"fangorn read failed: {err.decode(errors='replace').strip()}")
    data = json.loads(out)
    # Reject a degenerate read: `head: null` with no vertices means `onChainTip` came
    # back null (a flaky RPC / light-client read, or an on-chain head that isn't settled
    # yet), NOT an authoritatively-empty namespace. Treating it as a real seed would
    # (a) mark the source seeded and reuse an empty snapshot forever, and (b) diff every
    # previously-embedded vertex as "removed" and tombstone the whole collection. Raise
    # so the caller keeps the prior snapshot, goes live, and retries the seed next
    # reconnect. A genuinely empty namespace still returns a non-null head, so this only
    # rejects the broken case.
    if data.get("head") is None and not data.get("vertices"):
        raise RuntimeError("read returned null head + empty namespace (unsettled head or "
                           "flaky RPC read) — not seeding on this")
    return data


def _ns_state(snapshot, pair):
    """The in-memory {vertices, edges} for one (owner, namespace) pair. A wildcard source
    streams commits from many pairs, so state is per-pair, not per-subscription."""
    return snapshot["ns"].setdefault(pair, {"vertices": {}, "edges": {}})


async def _seed_pair(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                     checkpoint, owner, namespace, snapshot):
    """Read one namespace's full on-chain state and embed it, so the existing corpus is
    indexed before live diffs land on top. Bounded + off the event loop (see
    _seed_read_async): a slow/hung read degrades to 'skip the seed, go live' instead of
    freezing the watcher, and the pair stays unseeded so a later attempt retries it.

    On failure we leave the prior snapshot ALONE and ingest nothing: projecting an empty
    namespace would diff every already-embedded vertex as removed and tombstone the whole
    source (the exact outcome the null-head guard in _seed_read_async exists to prevent)."""
    key = f"{owner}:{namespace}"
    try:
        contents = await _seed_read_async(
            args.fangorn_bin, owner, namespace, args.seed_timeout, args.app)
    except Exception as e:  # noqa: BLE001
        print(f"[Watcher] {key}: seed read skipped ({e}); going live on the "
              f"stream, will retry seed on reconnect", file=sys.stderr)
        return

    state = _ns_state(snapshot, (owner, namespace))
    state["vertices"] = {v["cid"]: v for v in contents.get("vertices", [])}
    state["edges"] = {_edge_key(e): e for e in contents.get("edges", [])}

    seed_edges: list = []
    seed_tombstones: list = []
    n = await _ingest_contents(
        args, qdrant, embed_engine, role_map_ref, dim, truncate,
        checkpoint, owner, namespace, contents,
        edges_sink=seed_edges, tombstones_sink=seed_tombstones,
    )
    # Marked seeded only once the ingest actually landed. If _ingest_contents raised
    # (a Qdrant outage mid-upload), leaving this unset is what lets the next reconnect
    # retry the seed instead of reporting "reusing snapshot" over an empty collection.
    # `tried` in _stream_source_once bounds that to one attempt per pair per connection.
    snapshot["seeded"].add((owner, namespace))
    print(f"[Watcher] {key}: seeded — {n or 'no'} new record(s) embedded")
    # Scope the append to THIS source. One collection holds every watched namespace, so
    # an unfiltered append sweeps every other source's points into this domain.
    _deliver_cdn(args, qdrant, n, seed_edges, seed_tombstones,
                 owner=owner, namespace=namespace, app=args.app)


async def _stream_source_once(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                              checkpoint, owner, namespace, snapshot):
    """Seed one source (once), then consume its `fangorn subscribe` stream until it ends.
    Returns normally when the stream closes (caller resubscribes).

    `owner`/`namespace` may be None — a wildcard source (`*:*`, `0x..:*`, `*:ns`) that
    subscribes at the app level and receives commits from many namespaces. Those can't be
    seeded up front (there is no single namespace to read), so each pair is seeded lazily
    the first time a commit for it arrives.

    `snapshot` is the in-memory state ({seeded, ns}), owned by the caller so it PERSISTS
    across reconnects — the expensive full `fangorn read` seed runs only until it succeeds
    once per pair, then reconnects reuse the snapshot and rely on the subscribe cursor
    replaying any commits missed while down. Re-reading the whole namespace on every
    reconnect is what made a slow read freeze the watcher in a loop."""
    key = f"{owner or '*'}:{namespace or '*'}"
    app_mode = owner is None or namespace is None

    # Start the subscription FIRST so any commit that lands while we seed buffers in
    # the pipe. Applying such a buffered change after the seed is idempotent: vertices
    # are content-addressed (re-adding a cid the seed already has is a no-op) and the
    # checkpoint dedupes embeds — so the seed↔live gap loses nothing.
    proc = await asyncio.create_subprocess_exec(
        *subscribe_cmd(args.fangorn_bin, owner, namespace, args.from_start,
                       args.from_block),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_STREAM_LIMIT, env=app_env(args.app),
    )
    print(f"[Watcher] {key}: subscribed (pid {proc.pid})")
    stderr_task = asyncio.create_task(_drain_stderr(key, proc.stderr))

    try:
        # Seed current on-chain state ONCE per namespace so the existing corpus is embedded
        # before we go live. A pinned source seeds up front; a wildcard source has no single
        # namespace to read, so its pairs seed lazily below, on first commit. `tried` bounds
        # that to one attempt per pair per connection — a persistently failing read must not
        # cost --seed-timeout on every incoming change.
        tried: set = set()
        if not app_mode and (owner, namespace) not in snapshot["seeded"]:
            tried.add((owner, namespace))
            await _seed_pair(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                             checkpoint, owner, namespace, snapshot)
        elif not app_mode:
            print(f"[Watcher] {key}: reusing snapshot "
                  f"({len(_ns_state(snapshot, (owner, namespace))['vertices'])} vertices) "
                  f"— resuming stream")

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

            # Every change names the pair it belongs to — under a wildcard source that
            # varies line to line, so state, checkpoints and projection all key off the
            # change itself, not off this subscription's (possibly None) filter.
            ch_owner = change.get("owner") or owner
            ch_ns = change.get("namespace") or namespace
            ch_key = f"{ch_owner}:{ch_ns}"

            # First commit seen for a namespace we've never read: seed it now, so the
            # projection sees the whole graph and not just what streamed past since.
            # Applying the diff on top is idempotent (vertices are content-addressed;
            # removals of already-absent cids are no-ops).
            if (ch_owner, ch_ns) not in snapshot["seeded"] and (ch_owner, ch_ns) not in tried:
                tried.add((ch_owner, ch_ns))
                await _seed_pair(args, qdrant, embed_engine, role_map_ref, dim, truncate,
                                 checkpoint, ch_owner, ch_ns, snapshot)

            state = _ns_state(snapshot, (ch_owner, ch_ns))
            vertices_by_cid = state["vertices"]
            edges_by_key = state["edges"]

            # Apply the diff to the in-memory snapshot: removals first, then adds
            # (an add re-pointing a cid must win over a same-line removal of the old one).
            for cid in change.get("removedVertexCids", []):
                vertices_by_cid.pop(cid, None)
            for e in change.get("removedEdges", []):
                edges_by_key.pop(_edge_key(e), None)
            for v in change.get("addedVertices", []):
                vertices_by_cid[v["cid"]] = v
            for e in change.get("addedEdges", []):
                edges_by_key[_edge_key(e)] = e

            contents = {"vertices": list(vertices_by_cid.values()),
                        "edges": list(edges_by_key.values())}

            src_ck = checkpoint.setdefault("sources", {}).setdefault(ch_key, {})
            src_ck["head"] = change.get("newRoot")
            src_ck["block"] = change.get("blockNumber")

            print(f"[Watcher] {ch_key}: change @ block {change.get('blockNumber')} "
                  f"(+{len(change.get('addedVertices', []))} / "
                  f"-{len(change.get('removedVertexCids', []))} vertices) "
                  f"→ {change.get('commitCid')}")

            change_edges: list = []
            change_tombstones: list = []
            n = await _ingest_contents(
                args, qdrant, embed_engine, role_map_ref, dim, truncate,
                checkpoint, ch_owner, ch_ns, contents,
                edges_sink=change_edges, tombstones_sink=change_tombstones,
            )
            status = f"{n} new record(s) embedded" if n else "no new records for the active profiles"
            print(f"[Watcher] {ch_key}: change applied — {status}")
            _deliver_cdn(args, qdrant, n, change_edges, change_tombstones,
                         owner=ch_owner, namespace=ch_ns, app=args.app)

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
    key = f"{owner or '*'}:{namespace or '*'}"
    # Persist the in-memory namespace snapshots across reconnects so the expensive full
    # seed read runs only until it succeeds once per pair; later reconnects reuse them and
    # lean on the subscribe cursor to replay anything missed while down.
    snapshot = {"seeded": set(), "ns": {}}
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
# PER-SOURCE SCOPING — one process, many namespaces
# ---------------------------------------------------------------------------
def _slug(text: str) -> str:
    """Filename-safe fragment of a namespace/owner."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text or "").strip("-").lower() or "x"


_APP_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _app_slug(app: str) -> str:
    """The app's fragment of a domain name. An app id is sliced to its first 8 hex chars
    exactly as the owner address is — a full 66-char id would dominate the directory name
    for no extra safety. A plain name is slugged whole. Mirrors the worker's `appSlug()`."""
    return _slug(app[2:10]) if _APP_ID_RE.match(app or "") else _slug(app)


def _domain_for(app: str, owner: str, namespace: str) -> str:
    """CDN domain name for one watched source.

    Scoped by app AND owner AND namespace, because that triple IS the namespace: a bare
    subspace name collides the moment two publishers pick the same one (`music` is not
    rare), and one publisher can hold the same subspace in two apps. Either collision
    intermixes shards in a single domain.

    MUST match `domainFor()` in webworker/quickbeam-registry/src/index.js — the watcher
    names the directory and the worker names it BACK to filter a view's catalog, so byte
    drift between the two returns an empty catalog with no error on either side.
    """
    return f"{_app_slug(app)}-{_slug(owner[2:10])}-{_slug(namespace)}"


def _scoped_role_map(path: str, app: str, owner: str, namespace: str) -> str:
    root, ext = os.path.splitext(path)
    return f"{root}-{_app_slug(app)}-{_slug(owner[2:10])}-{_slug(namespace)}{ext or '.json'}"


def _source_args(args, app: str, owner: str | None, namespace: str | None,
                 pin_domain: bool = False):
    """A per-source view of `args`.

    Three settings must NOT be shared once a process watches several namespaces:

      app            each source names its own app, and it is what every chain read
                     resolves against (app_env → FANGORN_APP_ID). Leaving the global
                     --app here would read every source out of one app, so a view on
                     another app silently resolves nothing.
      cdn_domain     one domain per source, or their shards intermix.
      role_map_file  _ingest_contents re-infers the role map whenever the loaded one
                     doesn't fit the current records, then saves it — so two
                     dissimilar corpora sharing one file overwrite each other on
                     every single commit.

    `collection` deliberately stays shared: every point carries `owner`, `meta.app` and
    `meta.namespace`, so one collection is still attributable and `serve` can filter on
    it. `checkpoint_file` is shared too — its contents are keyed by the whole triple.

    `pin_domain` honours an explicit --cdn-domain, which only makes sense for a
    single static source (the pre-existing single-namespace invocation).
    """
    scoped = copy.copy(args)
    scoped.app = app
    if not pin_domain:
        # ponytail: a wildcard source gets no live CDN delivery. Its pairs are only
        # learned as commits arrive, so there is no single domain to bake at task
        # start — and _deliver_cdn ships to one fixed domain per task. Give the
        # delivery path a per-change domain if wildcard sources ever need shards.
        concrete = owner is not None and namespace is not None
        scoped.cdn_domain = (_domain_for(app, owner, namespace)
                             if args.cdn_dir and concrete else None)
    scoped.role_map_file = _scoped_role_map(args.role_map_file, app, owner or "*",
                                            namespace or "*")
    return scoped


def _bake_initial(args, qdrant, owner: str, namespace: str) -> None:
    """Bootstrap live CDN delivery for one source. append_domain can only EXTEND an
    already-baked domain, so bake once here to give it a base manifest; `cdn serve`
    then works immediately with no manual `cdn bake`.

    The spec is forced to this source rather than resolved from domains.json: the
    collection may hold other namespaces, and a bake-everything spec would pull them
    into this domain.
    """
    if not (args.cdn_dir and args.cdn_domain):
        return
    manifest_path = os.path.join(args.cdn_dir, args.cdn_domain, "manifest.json")
    if os.path.exists(manifest_path):
        print(f"[Watcher] CDN domain {args.cdn_domain!r} already baked — deltas append per change")
        return
    print(f"[Watcher] CDN domain {args.cdn_domain!r} not baked — baking initial snapshot...")
    try:
        from quickbeam.cdn import bake_domain
        entry = bake_domain(
            qdrant, args.collection, args.cdn_dir, args.cdn_domain,
            spec={"filter": {"app": [args.app], "owner": [owner],
                             "namespace": [namespace]}},
            config_path=args.cdn_config, model=args.embedding_model)
        print(f"[Watcher] initial CDN bake: {entry['count']} point(s) into "
              f"{args.cdn_dir}/{args.cdn_domain}")
    except Exception as e:  # noqa: BLE001 — delivery must never block ingest
        print(f"[Watcher] initial CDN bake failed: {e}", file=sys.stderr)


def _wild(value: str | None) -> str | None:
    """A source side, or None if it is a wildcard. `*`, empty and whitespace all mean
    "any" — the worker writes `*` for an unpinned side, and an absent key means the same."""
    text = (value or "").strip()
    return None if text in ("", "*") else text


def _show(source: tuple) -> str:
    """One source as `app:owner:namespace` for the log, wildcards as `*`."""
    return ":".join(part or "*" for part in source)


def _sort_key(source: tuple) -> tuple:
    """Order sources for the converge loop's `sorted()`. A wildcard side is None, which
    cannot be compared against str, so flatten it to "" (which also sorts wildcards first)."""
    return tuple(part or "" for part in source)


def _fetch_sources(url: str, default_app: str | None = None) -> set:
    """GET the watch list as a set of (app, owner, namespace) triples. Liberal about
    shape: a bare list or {"sources": [...]}, holding either "APP:OWNER:NAMESPACE"
    strings (or the older "OWNER:NAMESPACE", which takes `default_app`) or
    {app, owner, namespace} objects. A `*` or absent owner/namespace is a wildcard.

    The triple is the identity — dedupe on all three. One publisher can hold the same
    subspace name in two apps, and collapsing those to one key leaves the other app's
    namespace unwatched.

    An entry naming no app at all, with no --app to fall back on, is DROPPED rather than
    guessed: reads resolve against the app, so watching the wrong one silently indexes
    the wrong graph, which is worse than watching nothing.

    ponytail: urllib in a thread rather than adding an async HTTP client for one
    poll every --sources-refresh seconds.

    The User-Agent is NOT cosmetic: the registry worker sits behind Cloudflare, whose
    bot-signature check answers urllib's default `Python-urllib/x.y` with a 403 (error
    1010) before the worker ever runs. `/watchlist` itself is unauthenticated, so a 403
    here means the edge blocked us, not that we lack access.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "quickbeam-watch"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if isinstance(data, dict):
        data = data.get("sources", [])
    out = set()
    for item in data:
        if isinstance(item, str):
            parts = item.split(":")
            if len(parts) == 3:
                app, owner, namespace = parts
            elif len(parts) == 2:
                app, (owner, namespace) = default_app, parts
            else:
                continue
        elif isinstance(item, dict):
            app = item.get("app") or default_app
            owner, namespace = item.get("owner"), item.get("namespace")
        else:
            continue
        app = _wild(app) or _wild(default_app)
        if app:
            out.add((app, _wild(owner), _wild(namespace)))
    return out


def _make_qdrant(args) -> QdrantClient:
    """Qdrant connection: a remote URL (with optional API key) when given, else the
    local host/port pair. Mirrors pull.py's helper of the same name.

    ponytail: the URL branch does NOT set prefer_grpc. A remote Qdrant is typically
    reached through an HTTPS reverse proxy on 443, which does not carry gRPC on 6334 —
    forcing gRPC there fails to connect. REST is slower per upload and correct
    everywhere; switch it on if a deployment actually exposes gRPC.
    """
    if args.qdrant_url:
        return QdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key, timeout=600)
    return QdrantClient(host=args.qdrant_host, port=args.qdrant_port,
                        grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=600)


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
async def main():
    args = parse_args()

    sources = parse_sources(args.sources)
    if not sources and not args.sources_url:
        sys.exit("[Watcher] pass at least one --source OWNER:NAMESPACE, or --sources-url.")

    model_dim = MODEL_DIM_MAP.get(args.embedding_model, 768)
    dim       = min(args.dim, model_dim)
    truncate  = dim < model_dim

    shown = ", ".join(f"{args.app or '<default app>'}:{o or '*'}:{n or '*'}"
                      for o, n in sources)
    print(f"[Watcher] Starting — sources: {shown}")
    print(f"[Watcher] mode: push (fangorn subscribe); reconnect backoff {args.poll_interval}s")

    qdrant = _make_qdrant(args)

    if not qdrant.collection_exists(args.collection):
        qdrant.create_collection(
            args.collection,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE, on_disk=True)
        )
    ensure_indexes(qdrant, args.collection)

    # Init embed engine once and keep it alive across changes.
    embed_engine = _init_embed_engine(args)
    print(f"[Watcher] model dim={model_dim}, output dim={dim} (truncate={truncate})")

    checkpoint = _load_checkpoint(args.checkpoint_file)

    # One independent task per source, keyed by the whole (app, owner, namespace) triple.
    # Each gets its own scoped args — including its own app, so one process serves views
    # across several apps — and its own role map (see _source_args); the CDN domain is
    # baked once at task start.
    tasks: dict[tuple, asyncio.Task] = {}

    def start(app, owner, namespace, pin_domain=False):
        scoped = _source_args(args, app, owner, namespace, pin_domain=pin_domain)
        _bake_initial(scoped, qdrant, owner, namespace)
        return asyncio.create_task(
            _stream_source(scoped, qdrant, embed_engine, [{}], dim, truncate,
                           checkpoint, owner, namespace))

    # Static mode: fixed --source flags, all under the one --app.
    if not args.sources_url:
        pin = bool(args.cdn_domain) and len(sources) == 1
        for owner, namespace in sources:
            tasks[(args.app, owner, namespace)] = start(args.app, owner, namespace,
                                                        pin_domain=pin)
        await asyncio.gather(*tasks.values())
        return

    # Dynamic mode: converge on whatever the registry lists, forever. Each entry names
    # its own app, so one instance can serve views across several — --app is only the
    # fallback for an entry that names none.
    while True:
        try:
            wanted = _fetch_sources(args.sources_url, args.app)
        except Exception as e:  # noqa: BLE001
            # Keep every running source. A blip at the registry must never look like
            # "everyone unsubscribed" and tear the whole fleet down.
            print(f"[Watcher] sources fetch failed ({e}) — keeping current set",
                  file=sys.stderr)
            wanted = None

        if wanted is not None:
            for key in sorted(wanted - set(tasks), key=_sort_key):
                tasks[key] = start(*key)
                print(f"[Watcher] + {_show(key)} — now watching {len(tasks)} source(s)")
            for key in sorted(set(tasks) - wanted, key=_sort_key):
                tasks.pop(key).cancel()
                print(f"[Watcher] - {_show(key)} — now watching {len(tasks)} source(s)")

        # Surface a task that died of something _stream_source's own retry loop could
        # not absorb, instead of losing it silently.
        for key, task in list(tasks.items()):
            if task.done() and not task.cancelled():
                exc = task.exception()
                print(f"[Watcher] {_show(key)} stopped ({exc!r}) — restarting",
                      file=sys.stderr)
                tasks[key] = start(*key)

        await asyncio.sleep(args.sources_refresh)


if __name__ == "__main__":
    asyncio.run(main())
