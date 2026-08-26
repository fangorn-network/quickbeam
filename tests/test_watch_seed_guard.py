"""A namespace must never be silently emptied by the watcher.

Two ways that happened, both fixed in _stream_source_once:
  * a failed seed left an EMPTY in-memory snapshot, and the change was still
    re-projected against it — diffing every embedded vertex as "removed";
  * --from-block was suppressed once the CLI's cursor file existed, so a commit
    the child emitted but the watcher never ingested was never replayed.
"""
import asyncio
import inspect
import types

import pytest

from quickbeam import watcher


def test_failed_seed_does_not_ingest_the_change():
    """_seed_pair returns False on a failed read, and the caller must skip the change."""
    src = inspect.getsource(watcher._stream_source_once)
    assert 'if (ch_owner, ch_ns) not in snapshot["seeded"]:' in src, \
        "the unseeded-pair guard is gone — a failed seed will tombstone the namespace"
    # the guard must bail out before the change is applied to that empty snapshot
    after = src.split('if (ch_owner, ch_ns) not in snapshot["seeded"]:')[-1]
    assert after.index("continue") < after.index("state = _ns_state")


def test_seed_pair_reports_failure(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("read returned null head")
    monkeypatch.setattr(watcher, "_seed_read_async", boom)

    args = types.SimpleNamespace(fangorn_bin="fangorn", seed_timeout=1, app="app")
    snapshot = {"seeded": set(), "ns": {}}
    ok = asyncio.run(watcher._seed_pair(args, None, None, [{}], 256, True,
                                        {}, "0xowner", "ns", snapshot))
    assert ok is False
    assert ("0xowner", "ns") not in snapshot["seeded"]
    # and it must not have invented an empty snapshot to diff against
    assert snapshot["ns"] == {}


def test_from_block_survives_the_cursor_file():
    """The CLI's cursor advances when it EMITS; ours advances when we INGEST. Gating
    --from-block on the cursor loses every commit in between, permanently."""
    src = inspect.getsource(watcher._stream_source_once)
    assert "has_cursor" not in src, "--from-block is being suppressed by the cursor file again"
    assert "from_block = args.from_block" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
