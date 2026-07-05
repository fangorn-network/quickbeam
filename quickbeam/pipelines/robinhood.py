"""
Back-compat shim — the Robinhood pipeline moved to `quickbeam.ingest.scrapers`.

The source-specific shaper + read now live in `quickbeam.ingest.scrapers.robinhood`
(as `RobinhoodSource`); the generic ingest plumbing lives in
`quickbeam.ingest.scrapers.harness`. This module re-exports the public surface so
`cli.py` (`from quickbeam.pipelines.robinhood import run`), `pipelines/test_robinhood.py`,
and the `market-mesh` skill keep importing from here unchanged.

New code should import from `quickbeam.ingest.scrapers.robinhood` directly.
"""
from __future__ import annotations

from quickbeam.ingest.scrapers import harness as _harness
from quickbeam.ingest.scrapers.robinhood import (  # noqa: F401
    ENTITY_TYPES,
    ROBINHOOD_BLOCKSCOUT,
    ROBINHOOD_CHAIN_ID,
    ROBINHOOD_PRESENTATION,
    ROBINHOOD_ROLE_MAP,
    ROBINHOOD_RPC_URL,
    RobinhoodSource,
    _iso_to_epoch,
    _num,
    _pct,
    _signal,
    build_graph,
    node_id,
    read_robinhood_events,
    run,
    shape_event,
    shape_fields,
    verbalize,
)

_SOURCE = RobinhoodSource()


def compose_searchable_text(fields: dict, role_map: dict = ROBINHOOD_ROLE_MAP) -> str:
    """Back-compat wrapper — the composer is now source-agnostic in the harness and
    takes an explicit role_map (defaulted here to Robinhood's for legacy callers)."""
    return _harness.compose_searchable_text(fields, role_map)


def emit_volumes(events: list[dict], output_dir: str, volume: int = 1,
                 accumulate: bool = False) -> dict:
    """Back-compat wrapper — the harness `emit_volumes` is source-parameterized; this
    binds it to `RobinhoodSource` so the old `emit_volumes(events, dir, …)` call works."""
    return _harness.emit_volumes(_SOURCE, events, output_dir, volume, accumulate)


if __name__ == "__main__":
    run()
