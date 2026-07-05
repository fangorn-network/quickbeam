"""
The `Source` contract — the pluggable ingestion adapter.

A scraper author implements THREE required methods (`read`, `build_graph`,
`next_cursor`) plus `add_source_args`, and declares a handful of attributes. The
harness (`quickbeam.ingest.scrapers.harness`) owns everything else: shared argparse,
volume emission, checkpointing, the `--watch` poll daemon, and `--publish` to fangorn.

`Source` is a `typing.Protocol` on purpose — an out-of-tree implementer structurally
satisfies it without importing/inheriting anything from quickbeam, so third-party
scraper packages carry no hard dependency on this class. In-tree adapters may subclass
`SourceBase` (below) purely for the attribute defaults; that is a convenience, not part
of the contract.

THE DATA CONTRACT
-----------------
`build_graph` returns the exact shape `schemagen` → `fangorn commit --bundle` consumes:

    nodes:  {entity_type: [{"name": <stable id>, "fields": {...}}, ...]}
    edges:  [{"rel": <relation>, "from": <node name>, "to": <node name>,
              "fromType": <entity>, "toType": <entity>}, ...]

`fields` carries a `text` blurb (what gets embedded) plus structured measures indexed
for hybrid filtering. Node `name` is a STABLE id — snapshot nodes reuse one id per
logical entity (latest wins, upsert), discrete events get a unique id each.
"""
from __future__ import annotations

import argparse
from typing import Protocol, runtime_checkable


@runtime_checkable
class Source(Protocol):
    # ── identity + presentation ────────────────────────────────────────────────
    name: str
    """The `data <name>` verb and the log/ checkpoint-key prefix (e.g. "robinhood")."""

    snapshot_stems: set[str]
    """Volume stems rewritten wholesale every cycle (latest wins), vs. every other
    stem which is accumulated into a growing ledger under `--accumulate`. Assets are
    live quotes keyed on a stable id → snapshot; discrete events → ledger."""

    role_map: dict
    """title/subtitle/tags/text field roles — drives the `--dry-run` embed preview."""

    presentation: dict
    """Accent color + per-type icons (display metadata carried through to the CDN)."""

    stems: dict
    """entity_type → volume stem override. Missing entities default to `entity.lower()`."""

    # ── optional attributes (harness reads via getattr with these defaults) ─────
    #   edges_stem: str = "edges"      # the volume_<n>_<edges_stem>.json filename
    #   default_volume: int = 1        # the --volume default for this source
    # A batch source (no live tail) simply returns `prev` from next_cursor; the
    # harness's checkpoint/watch/accumulate machinery then no-ops harmlessly.

    # ── behavior ───────────────────────────────────────────────────────────────
    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        """Register source-only flags (--rpc-url, --bbox, --dsn, cursor floors …).
        The harness has already added the shared flags (--output-dir/--volume/--watch/
        --poll-interval/--accumulate/--checkpoint-file + the publish group)."""
        ...

    def read(self, cursor: int, args: argparse.Namespace) -> list[dict]:
        """Pull raw records from the upstream source. `cursor` is the persisted
        checkpoint (0 if none); the source combines it with its own floor flags to
        decide what to (re-)emit. All network/DB IO lives here and nowhere else."""
        ...

    def build_graph(self, records: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
        """records → (nodes_by_entity_type, edges) in the data-contract shape above.
        Pure; no IO — this is the unit-testable core."""
        ...

    def next_cursor(self, records: list[dict], prev: int) -> int:
        """The checkpoint value to persist after this cycle. Returns `prev` unchanged
        when nothing advances it (a snapshot-only read never moves the cursor)."""
        ...


class SourceBase:
    """Optional convenience base supplying attribute defaults. Subclassing is NOT
    required — any object structurally matching `Source` works. Adapters typically
    set `name`/`snapshot_stems`/`role_map`/`presentation`/`stems` as class attributes
    and implement the four methods."""

    name: str = ""
    snapshot_stems: set[str] = set()
    role_map: dict = {}
    presentation: dict = {}
    stems: dict = {}
    edges_stem: str = "edges"
    default_volume: int = 1
