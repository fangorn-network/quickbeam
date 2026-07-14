"""
The ingestion harness — the source-agnostic runtime every `Source` runs on.

Owns the shared CLI, staged-volume emission, incremental checkpointing, the
`--watch` poll daemon, and `--publish` to fangorn. A `Source` (see `source.py`)
supplies only read + shape + cursor; `run_source(src)` wires it up.

Pipeline position: this stages `volume_{N}_{stem}.json` node/edge files, then
`--publish` assembles them into one batch and writes it into the configured wallet's
namespace with `fangorn repo init <namespace>` + `fangorn upload <namespace> <batch>`.
`quickbeam watch --source <owner>:<namespace>` reads that namespace back off-chain and
embeds it. This module never embeds and never touches the CDN.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

from .source import Source


# ---------------------------------------------------------------------------
# SHARED CLI — every source gets these flags; the source adds its own (read
# params + cursor floors) via `src.add_source_args`, registered first.
# ---------------------------------------------------------------------------
def build_parser(src: Source) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"quickbeam data {src.name}",
        description=f"Transform {src.name} events into staged node/edge volumes. "
                    f"Publish with `fangorn repo init` + `fangorn upload`, then embed "
                    f"with `quickbeam watch --source <owner>:<namespace>`.")
    src.add_source_args(p)

    p.add_argument("--checkpoint-file", default=None,
                   help="Persist the ingest cursor to this JSON file and resume above it "
                        "each cycle, so the live tail never re-emits or misses across "
                        "restarts. NOTE: this is the INGEST checkpoint; `watch "
                        "--checkpoint-file` is a separate embed-side cursor.")
    p.add_argument("--accumulate", action="store_true",
                   help="Grow a LEDGER instead of overwriting: merge new rows/edges into "
                        "the staged files (dedup by id, old rows kept) so each fangorn "
                        "commit is a superset of the last. This is what lets the watcher's "
                        "index grow past the current snapshot — without it, every commit "
                        "replaces prior flow and the watcher's delete-propagation collapses "
                        "the index back to one snapshot. Snapshot stems are always rewritten "
                        "(latest wins). Pair with --checkpoint-file so a restart resumes.")
    p.add_argument("--output-dir", default="./stage_volumes",
                   help="Where to write volume_<n>_*.json node/edge files.")
    p.add_argument("--volume", type=int, default=getattr(src, "default_volume", 1),
                   help="Volume number (coexists with other pipelines' volumes).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the shaped nodes + their embed text; write nothing.")

    # ── Live ingest daemon ─────────────────────────────────────────────────────
    p.add_argument("--watch", action="store_true",
                   help="Run as a long-lived daemon: re-read the source every "
                        "--poll-interval seconds and re-emit (and, with --publish, "
                        "re-commit) the snapshot. Also runnable one-shot from cron.")
    p.add_argument("--poll-interval", type=int, default=120,
                   help="Seconds between reads in --watch mode (default 120).")

    pub = p.add_argument_group("publish to fangorn (optional — the ingest→publish leg)")
    pub.add_argument("--publish", action="store_true",
                     help="After writing volumes, assemble them into one batch and run "
                          "`fangorn repo init <namespace>` (idempotent) + `fangorn commit "
                          "-m <msg>` + `fangorn push` to settle the snapshot into the "
                          "configured wallet's namespace.")
    pub.add_argument("--namespace", default=None,
                     help="Fangorn namespace to publish into (required with --publish). "
                          "`quickbeam watch --source <owner>:<namespace>` reads it back.")
    pub.add_argument("--fangorn-bin", default="fangorn",
                     help="The fangorn CLI invocation — a full command, not just a path "
                          "(shell-split). Default `fangorn` (a global install reading "
                          "~/.fangorn/config.json). For the git-native dev build use its "
                          "wrapper, e.g. \"dotenvx run -f ~/fangorn/fangorn/.env -- node "
                          "~/fangorn/fangorn/dist/src/cli/cli.js\".")
    return p


# ---------------------------------------------------------------------------
# DRY-RUN PREVIEW — mirrors embeddings._embed_and_upload's `auto` composer so
# `--dry-run` shows what the Path A loop would embed, with no fastembed/Qdrant.
# ---------------------------------------------------------------------------
def compose_searchable_text(fields: dict, role_map: dict) -> str:
    tags = " ".join(str(fields.get(t, "")) for t in role_map.get("tags", [])
                    if isinstance(fields.get(t), str))
    subtitle = fields.get(role_map.get("subtitle", ""), "")
    text_terms = "; ".join(str(fields[t]) for t in (role_map.get("text", []) or [])
                           if fields.get(t))
    s = f"Title: {fields.get(role_map.get('title', ''), '')}. Tags: {tags}"
    if subtitle:
        s += f". Subtitle: {subtitle}"
    if text_terms:
        s += f". {text_terms}"
    return f"search_document: {s[:1000]}"


def _print_dry_run(src: Source, records: list[dict]) -> None:
    nodes, edges = src.build_graph(records)
    for entity, node_list in nodes.items():
        print(f"\n[{entity}] {len(node_list)} node(s)")
        for n in node_list[:4]:
            print(f"  · {n['name']}")
            print(f"    embed → {compose_searchable_text(n['fields'], src.role_map)}")
    print(f"\n[edges] {len(edges)}: " + ", ".join(sorted({e['rel'] for e in edges})))


# ---------------------------------------------------------------------------
# STAGED-VOLUME EMISSION — write the node/edge files the publish leg reads back.
# ---------------------------------------------------------------------------
def _load_node_list(path: str) -> list[dict]:
    """Existing staged nodes/edges (empty list if absent/unreadable). A corrupt
    staged file must not stall ingest — we start that stem fresh instead."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception as e:  # noqa: BLE001
        print(f"[ingest] staged file {path!r} unreadable ({e}); starting stem fresh",
              file=sys.stderr)
        return []


def _merge_keep_order(existing: list[dict], new: list[dict], key) -> tuple[list[dict], int]:
    """Append rows from `new` whose key isn't already present, preserving the
    existing order (so fangorn's content-addressed chunking reuses unchanged
    chunks — old rows stay byte-identical). Returns (merged, added_count)."""
    seen = {key(r) for r in existing}
    merged = list(existing)
    added = 0
    for r in new:
        k = key(r)
        if k not in seen:
            seen.add(k)
            merged.append(r)
            added += 1
    return merged, added


def _stem_for(src: Source, entity: str) -> str:
    return getattr(src, "stems", {}).get(entity, entity.lower())


def emit_volumes(src: Source, records: list[dict], output_dir: str, volume: int = 1,
                 accumulate: bool = False) -> dict:
    """INGEST OUTPUT — write the staged node/edge volume files the publish leg reads
    back into a `fangorn upload` batch. Returns a per-type count summary.

    accumulate=True turns non-snapshot stems into a growing ledger: new rows are
    MERGED into the staged file rather than overwriting it, so each commit is a
    superset and the watcher never tombstones prior flow. Snapshot stems
    (`src.snapshot_stems`) are always rewritten wholesale (latest wins)."""
    os.makedirs(output_dir, exist_ok=True)
    nodes, edges = src.build_graph(records)
    snapshot_stems = src.snapshot_stems
    counts: dict = {}
    written_stems = set()
    for entity, node_list in nodes.items():
        stem = _stem_for(src, entity)
        written_stems.add(stem)
        path = os.path.join(output_dir, f"volume_{volume}_{stem}.json")
        if accumulate and stem not in snapshot_stems:
            merged, added = _merge_keep_order(
                _load_node_list(path), node_list, lambda r: r.get("name"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(merged, f)
            counts[entity] = len(merged)
            print(f"   ✅ {entity:<18}: {len(merged):,} (+{added:,} new) → "
                  f"{os.path.basename(path)}")
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(node_list, f)
            counts[entity] = len(node_list)
            print(f"   ✅ {entity:<18}: {len(node_list):,} → {os.path.basename(path)}")
    edges_stem = getattr(src, "edges_stem", "edges")
    epath = os.path.join(output_dir, f"volume_{volume}_{edges_stem}.json")
    if accumulate:
        merged_e, added_e = _merge_keep_order(
            _load_node_list(epath), edges,
            lambda e: (e.get("rel"), e.get("from"), e.get("to")))
        with open(epath, "w", encoding="utf-8") as f:
            json.dump(merged_e, f)
        counts["edges"] = len(merged_e)
        print(f"   ✅ {'edges':<18}: {len(merged_e):,} (+{added_e:,} new) → "
              f"{os.path.basename(epath)}")
    else:
        with open(epath, "w", encoding="utf-8") as f:
            json.dump(edges, f)
        counts["edges"] = len(edges)
        print(f"   ✅ {'edges':<18}: {len(edges):,} → {os.path.basename(epath)}")
    # Remove stale type-volumes for this volume that this run didn't write (e.g. a
    # prior run left volume_N_transfers.json, now empty) so a commit never picks up
    # stale data. Only touches THIS source's known stems — never another pipeline's
    # files sharing the dir. Under --accumulate the ledger stems persist across cycles,
    # so only prune snapshot stems (which never go stale).
    all_stems = set(getattr(src, "stems", {}).values())
    prunable = ({s for s in all_stems if s in snapshot_stems}
                if accumulate else all_stems)
    for stem in prunable - written_stems:
        stale = os.path.join(output_dir, f"volume_{volume}_{stem}.json")
        if os.path.exists(stale):
            os.remove(stale)
            print(f"   🧹 removed stale {os.path.basename(stale)}")
    return counts


# ---------------------------------------------------------------------------
# CHECKPOINT — the ingest cursor. Keyed per source so pointing two sources at one
# checkpoint file is safe; the writer MERGES, so foreign keys survive untouched.
# Deliberately distinct from the watcher's embed-side checkpoint.
# ---------------------------------------------------------------------------
def _ckpt_key(src: Source) -> str:
    return f"{src.name}IngestBlock"


def _read_json_obj(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:  # noqa: BLE001 — a bad checkpoint must not stall ingest
        print(f"[ingest] checkpoint {path!r} unreadable ({e}); starting from floor",
              file=sys.stderr)
        return {}


def load_checkpoint(src: Source, path: str | None) -> int:
    """Highest cursor previously ingested (0 if none/unset)."""
    return int(_read_json_obj(path).get(_ckpt_key(src), 0) or 0)


def _merge_json_file(path: str | None, updates: dict) -> None:
    """Read-merge-write `updates` into the JSON object at `path` (atomically), never
    clobbering foreign keys already there. Shared by the checkpoint cursor and the
    freshness block so two sources — or the cursor + freshness of one source — can
    coexist in one file. A crashed write never truncates the file (write-tmp-rename)."""
    if not path:
        return
    d = _read_json_obj(path)      # merge into whatever is there — never clobber foreign keys
    d.update(updates)
    # Ensure the dir exists (e.g. a fresh `db/`), or the .tmp write below raises
    # FileNotFoundError and stalls every cycle.
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)  # atomic — a crashed write never truncates the file


def save_checkpoint(src: Source, path: str | None, cursor: int) -> None:
    _merge_json_file(path, {_ckpt_key(src): int(cursor)})


def save_freshness(src: Source, path: str | None, report: dict) -> None:
    """Persist the freshness metrics alongside the cursor under `<name>Freshness`, so
    the checkpoint file is a single place to read 'where is the tail in time?'. The
    `display` lines are dropped (they're a render, not state)."""
    _merge_json_file(path, {f"{src.name}Freshness":
                            {k: v for k, v in report.items() if k != "display"}})


# ---------------------------------------------------------------------------
# INGEST — one cycle, and the daemon that loops it.
# ---------------------------------------------------------------------------
def _report_freshness(src: Source, records: list[dict], cursor: int, args) -> None:
    """Print (and, outside --dry-run, persist) the source's freshness summary if it
    supplies the optional `freshness_report` hook. Purely informational — a failure
    here never aborts the cycle."""
    fn = getattr(src, "freshness_report", None)
    if not callable(fn):
        return
    try:
        report = fn(records, cursor)
    except Exception as e:  # noqa: BLE001 — a report is never worth killing a cycle for
        print(f"[{src.name}] freshness report failed: {e}", file=sys.stderr)
        return
    if not report:
        return
    for line in report.get("display", []):
        print(line)
    if not args.dry_run and args.checkpoint_file:
        save_freshness(src, args.checkpoint_file, report)


def ingest_once(src: Source, args) -> int:
    """One read → emit volumes (→ optionally publish to fangorn). Returns record count.
    This is the unit a cron job runs each tick, and the daemon runs each cycle."""
    cursor = load_checkpoint(src, args.checkpoint_file)
    records = src.read(cursor, args)
    if not records:
        print(f"[{src.name}] no events to ingest")
        return 0
    _report_freshness(src, records, cursor, args)
    if args.dry_run:
        _print_dry_run(src, records)
        return len(records)
    print(f"[{src.name}] ingesting {len(records)} event(s) → {args.output_dir} "
          f"(volume {args.volume})")
    emit_volumes(src, records, args.output_dir, args.volume, accumulate=args.accumulate)
    # Publish BEFORE advancing the checkpoint, and advance only on a successful publish
    # (or when not publishing), so a failed publish is retried next cycle rather than
    # silently skipped by a cursor that already moved past it.
    published = True
    if args.publish:
        published = _publish_to_fangorn(src, args)
    else:
        print(f"[{src.name}] next: `fangorn repo init <namespace>` → `fangorn upload "
              "<namespace> <batch>` → `quickbeam watch --source <owner>:<namespace> …`  "
              "(or add --publish to publish now)")
    if published:
        nxt = src.next_cursor(records, cursor)
        if nxt > cursor:
            save_checkpoint(src, args.checkpoint_file, nxt)
    return len(records)


def watch_ingest(src: Source, args) -> None:
    """Long-lived ingest daemon: re-read the source every --poll-interval seconds and
    re-emit (and, with --publish, re-commit) the current snapshot."""
    print(f"[{src.name}] ingest daemon — out={args.output_dir} (volume {args.volume}), "
          f"publish={args.publish}, poll={args.poll_interval}s, "
          f"mode={'ledger (accumulate)' if args.accumulate else 'snapshot (replace)'}"
          + (f" (checkpoint {args.checkpoint_file})" if args.checkpoint_file else ""))
    cycle = 0
    while True:
        cycle += 1
        print(f"\n[{src.name}] ── ingest cycle {cycle} ──")
        try:
            ingest_once(src, args)
        except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the daemon
            print(f"[{src.name}] cycle {cycle} error: {e}", file=sys.stderr)
        print(f"[{src.name}] sleeping {args.poll_interval}s…")
        time.sleep(args.poll_interval)


# ---------------------------------------------------------------------------
# FANGORN CLI DRIVERS — reusable subprocess helpers shared by the harness's
# `--publish` leg and the programmatic `quickbeam.Publisher` façade. Each shells
# to the fangorn CLI (`--fangorn-bin` may be a FULL command, not just a path — e.g.
# the dev invocation `dotenvx run -f /abs/.env -- node /abs/dist/src/cli/cli.js` —
# so it is shell-split). `tag` is only a log prefix.
#
# The publish leg is owner:namespace and follows fangorn's git-native model: assemble
# the staged volumes into one batch {vertices, edges}, then
#   fangorn repo init <namespace>   (idempotent — tracks the namespace, allocates if new)
#   fangorn commit <batch> -m <msg> (snapshots the batch into a LOCAL commit)
#   fangorn push                    (settles that commit as the on-chain state root)
# There is no schema registration and no `upload` verb — commit is local, push is the
# permissioned on-chain write that `fangorn read`/`subscribe` (what `watch --source`
# consumes) resolve back. All three steps share one CWD: `repo init` writes the
# `.fangorn/repo.json` pointer there and `commit`/`push` resolve the repo by searching
# upward from CWD, so they MUST run in the same directory.
# ---------------------------------------------------------------------------
def _run_fangorn_steps(steps: list[tuple[str, list[str]]], *,
                       fangorn_bin: str, tag: str, cwd: str | None = None) -> bool:
    """Run a sequence of (label, argv-tail) fangorn steps. Returns False on the first
    failure (missing CLI or non-zero exit), True if all succeed."""
    prefix = shlex.split(fangorn_bin)
    for label, tail in steps:
        cmd = [*prefix, *tail]
        print(f"[{tag}] fangorn {label}: {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, cwd=cwd)
        except FileNotFoundError:
            print(f"[{tag}] fangorn CLI not found (fangorn-bin {fangorn_bin!r}, resolved "
                  f"to {prefix[0]!r}). Install the git-native fangorn or pass its full "
                  f"invocation, e.g. \"dotenvx run -f ~/fangorn/fangorn/.env -- node "
                  f"~/fangorn/fangorn/dist/src/cli/cli.js\".", file=sys.stderr)
            return False
        if r.returncode != 0:
            print(f"[{tag}] fangorn {label} failed (exit {r.returncode})", file=sys.stderr)
            return False
    return True


def _load_staged_volume(src: Source, output_dir: str,
                        volume: int) -> tuple[dict[str, list[dict]], list[dict]]:
    """Read back the volume_<N>_*.json files emit_volumes wrote, inverting the
    entity→stem mapping (`src.stems`). Returns ({entityType: [{"name","fields"}]},
    [edge...]) — the shape the publish leg turns into a `fangorn upload` batch."""
    stem_to_entity = {stem: entity for entity, stem in getattr(src, "stems", {}).items()}
    edges_stem = getattr(src, "edges_stem", "edges")
    nodes_by_entity: dict[str, list[dict]] = {}
    if not os.path.isdir(output_dir):
        return nodes_by_entity, []
    prefix = f"volume_{volume}_"
    edges_file = f"{prefix}{edges_stem}.json"
    for fname in sorted(os.listdir(output_dir)):
        if not (fname.startswith(prefix) and fname.endswith(".json")) or fname == edges_file:
            continue
        stem = fname[len(prefix):-len(".json")]
        entity = stem_to_entity.get(stem, stem.capitalize())
        nodes_by_entity[entity] = _load_node_list(os.path.join(output_dir, fname))
    edges = _load_node_list(os.path.join(output_dir, edges_file))
    return nodes_by_entity, edges


def _repo_dir(args) -> str:
    """Stable per-namespace working directory for the fangorn repo pointer. `repo init`
    drops `.fangorn/repo.json` here and `commit`/`push` resolve it by searching upward,
    so the three steps must share this CWD. Kept separate per namespace so distinct
    sources publishing under one output-dir don't clobber each other's pointer. HEAD is
    reconstructed from the on-chain tip by `repo init`, so this dir is restart-safe."""
    d = os.path.join(args.output_dir, ".fangorn-repos", args.namespace)
    os.makedirs(d, exist_ok=True)
    return d


def fangorn_repo_init(*, namespace: str, fangorn_bin: str, cwd: str,
                      tag: str = "publish") -> bool:
    """`fangorn repo init <namespace>` — bootstrap/track the wallet's namespace (writes
    `.fangorn/repo.json` in `cwd`). Idempotent: if the namespace already exists it just
    re-tracks it (HEAD ← on-chain tip), so it's safe to run every cycle."""
    return _run_fangorn_steps(
        [("repo init", ["repo", "init", namespace])],
        fangorn_bin=fangorn_bin, tag=tag, cwd=cwd)


def fangorn_commit_push(*, batch_path: str, message: str, fangorn_bin: str, cwd: str,
                        tag: str = "publish") -> bool:
    """`fangorn commit <batch_path> -m <message>` (local) then `fangorn push` (on-chain).
    Must run in the same `cwd` as `fangorn_repo_init`. `batch_path` is a JSON file
    `{vertices:[{id,tag,payload}], edges:[{rel,from,to}]}` (an absolute path, so CWD
    doesn't affect which file is read)."""
    return _run_fangorn_steps(
        [("commit", ["commit", batch_path, "-m", message]),
         ("push", ["push"])],
        fangorn_bin=fangorn_bin, tag=tag, cwd=cwd)


def _publish_to_fangorn(src: Source, args) -> bool:
    """The harness `--publish` leg: assemble the just-written volumes into one batch and
    settle it into the configured wallet's namespace via `fangorn repo init` +
    `commit -m` + `push` (all in one per-namespace repo dir)."""
    if not getattr(args, "namespace", None):
        print(f"[{src.name}] --publish requires --namespace. Skipping publish.",
              file=sys.stderr)
        return False

    nodes_by_entity, edges = _load_staged_volume(src, args.output_dir, args.volume)
    vertices = [
        {"id": n["name"], "tag": entity, "payload": n["fields"]}
        for entity, node_list in nodes_by_entity.items()
        for n in node_list
    ]
    edge_records = [{"rel": e["rel"], "from": e["from"], "to": e["to"]} for e in edges]
    if not vertices:
        print(f"[{src.name}] --publish: nothing staged to publish.", file=sys.stderr)
        return False

    cwd = _repo_dir(args)
    message = (f"quickbeam:{src.name} v{args.volume} — "
               f"{len(vertices)} vertices, {len(edge_records)} edges")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"vertices": vertices, "edges": edge_records}, f)
        batch_path = f.name
    try:
        ok = (fangorn_repo_init(namespace=args.namespace, fangorn_bin=args.fangorn_bin,
                                cwd=cwd, tag=src.name)
              and fangorn_commit_push(batch_path=batch_path, message=message,
                                      fangorn_bin=args.fangorn_bin, cwd=cwd, tag=src.name))
    finally:
        try:
            os.unlink(batch_path)
        except OSError:
            pass
    if ok:
        print(f"[{src.name}] published {len(vertices)} vertice(s), {len(edge_records)} "
              f"edge(s) to namespace {args.namespace!r} ✓")
    return ok


def run_source(src: Source, argv: list[str] | None = None) -> None:
    """The entrypoint each adapter's `run()` delegates to: parse args, then dispatch
    to the one-shot ingest or the watch daemon."""
    args = build_parser(src).parse_args(argv)
    if args.watch:
        watch_ingest(src, args)
    else:
        ingest_once(src, args)
