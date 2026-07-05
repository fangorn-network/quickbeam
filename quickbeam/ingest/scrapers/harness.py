"""
The ingestion harness — the source-agnostic runtime every `Source` runs on.

Lifted (behaviour-preserving) from the plumbing that grew inside
`pipelines/robinhood.py`. Owns the shared CLI, staged-volume emission, incremental
checkpointing, the `--watch` poll daemon, and `--publish` to fangorn. A `Source`
(see `source.py`) supplies only read + shape + cursor; `run_source(src)` wires it up.

Pipeline position (unchanged): this stages `volume_{N}_{stem}.json` node/edge files
that `schemagen` → `fangorn commit --bundle` → `push` publishes on-chain; `watch`
then embeds the committed tip. This module never embeds and never touches the CDN.
"""
from __future__ import annotations

import argparse
import datetime
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
                    f"Publish with `fangorn commit --bundle`, then embed with "
                    f"`quickbeam watch --bundle`.")
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
                     help="After writing volumes, run `fangorn commit --bundle` + "
                          "`fangorn push` to publish the snapshot on-chain. The repo "
                          "must be `fangorn repo init`'d against the bundle schema first.")
    pub.add_argument("--repo", default=".",
                     help="Fangorn repo dir to run commit/push in (its cwd).")
    pub.add_argument("--fangorn-bin", default="fangorn",
                     help="The fangorn CLI invocation — a full command, not just a path "
                          "(shell-split). Default `fangorn` (a global install reading "
                          "~/.fangorn/config.json). For the git-native dev build use its "
                          "wrapper, e.g. \"dotenvx run -f ~/fangorn/fangorn/.env -- node "
                          "~/fangorn/fangorn/dist/src/cli/cli.js\".")
    pub.add_argument("--commit-message", default=None,
                     help="Commit message (default: a timestamped snapshot message).")
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
# STAGED-VOLUME EMISSION — write the node/edge files `schemagen` reads.
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
    """INGEST OUTPUT — write the staged node/edge volume files `schemagen` reads.
    Returns a per-type count summary.

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


def save_checkpoint(src: Source, path: str | None, cursor: int) -> None:
    if not path:
        return
    d = _read_json_obj(path)      # merge into whatever is there — never clobber foreign keys
    d[_ckpt_key(src)] = int(cursor)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)  # atomic — a crashed write never truncates the checkpoint


# ---------------------------------------------------------------------------
# INGEST — one cycle, and the daemon that loops it.
# ---------------------------------------------------------------------------
def ingest_once(src: Source, args) -> int:
    """One read → emit volumes (→ optionally publish to fangorn). Returns record count.
    This is the unit a cron job runs each tick, and the daemon runs each cycle."""
    cursor = load_checkpoint(src, args.checkpoint_file)
    records = src.read(cursor, args)
    if not records:
        print(f"[{src.name}] no events to ingest")
        return 0
    if args.dry_run:
        _print_dry_run(src, records)
        return len(records)
    print(f"[{src.name}] ingesting {len(records)} event(s) → {args.output_dir} "
          f"(volume {args.volume})")
    emit_volumes(src, records, args.output_dir, args.volume, accumulate=args.accumulate)
    # Advance the checkpoint. After emit_volumes so --dry-run stays side-effect-free.
    nxt = src.next_cursor(records, cursor)
    if nxt > cursor:
        save_checkpoint(src, args.checkpoint_file, nxt)
    if args.publish:
        _publish_to_fangorn(src, args)
    else:
        print(f"[{src.name}] next: `quickbeam data schemagen` → `fangorn commit "
              "--bundle` → `quickbeam watch --bundle …`  (or add --publish to commit now)")
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


def _publish_to_fangorn(src: Source, args) -> bool:
    """Publish the just-written volumes on-chain: `fangorn commit --bundle` + `fangorn
    push`, run in the repo dir. This is the ingest→publish leg — the on-chain write
    that emits the DataSource events a subgraph indexes and `watch --bundle` reads."""
    # Preflight: commit --bundle only works inside an initialized repo. Fail clearly
    # once instead of a confusing non-zero exit every cycle.
    if not os.path.isdir(os.path.join(args.repo, ".fangorn")):
        print(f"[{src.name}] --publish: {args.repo!r} is not a fangorn repo (no .fangorn/). "
              f"Bootstrap once: cd there and `fangorn repo init <name> -s <bundleSchema>`. "
              f"Skipping publish.", file=sys.stderr)
        return False
    msg = args.commit_message or (
        f"{src.name} snapshot "
        + datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    # --fangorn-bin may be a full command, not just an executable — e.g. the dev
    # invocation `dotenvx run -f /abs/.env -- node /abs/dist/src/cli/cli.js`. Split it
    # so the subcommand/args append cleanly.
    prefix = shlex.split(args.fangorn_bin)
    steps = [
        ("commit", [*prefix, "commit", "--bundle",
                    os.path.abspath(args.output_dir), "--volume", str(args.volume),
                    "-m", msg]),
        ("push", [*prefix, "push"]),
    ]
    for label, cmd in steps:
        print(f"[{src.name}] fangorn {label} (cwd={args.repo}): {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, cwd=args.repo)
        except FileNotFoundError:
            print(f"[{src.name}] fangorn CLI not found (--fangorn-bin {args.fangorn_bin!r}, "
                  f"resolved to {prefix[0]!r}). Install the git-native fangorn or pass its "
                  f"full invocation, e.g. --fangorn-bin \"dotenvx run -f "
                  f"~/fangorn/fangorn/.env -- node ~/fangorn/fangorn/dist/src/cli/cli.js\". "
                  f"Skipping publish.", file=sys.stderr)
            return False
        if r.returncode != 0:
            print(f"[{src.name}] fangorn {label} failed (exit {r.returncode})",
                  file=sys.stderr)
            return False
    print(f"[{src.name}] published snapshot to fangorn ✓")
    return True


def run_source(src: Source, argv: list[str] | None = None) -> None:
    """The entrypoint each adapter's `run()` delegates to: parse args, then dispatch
    to the one-shot ingest or the watch daemon."""
    args = build_parser(src).parse_args(argv)
    if args.watch:
        watch_ingest(src, args)
    else:
        ingest_once(src, args)
