"""The streaming seed + structured-bypass path in watcher._stream_source_once.

Drives the REAL seed routing with fakes (no fangorn subprocess, no Qdrant, no GPU):
a synthetic `fangorn read --ndjson` stream of an embedded Asset+Transfer graph plus
edgeless structured PriceBars, then one live change adding another bar. Asserts:

  1. structured (edgeless) rows are indexed with the constant placeholder vector and
     NEVER pass through the embed model — so 780k bars can't OOM the model/seed;
  2. the small embedded graph stays resident and folds neighbors (Asset ← Transfer);
  3. NO tombstone delete fires on the first live change — the regression guard: if
     structured rows leaked into the resident snapshot, the diff would see them all as
     "removed" and wipe the collection.

Run: `python -m pytest tests/test_streaming_seed.py`  (or run this file directly)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import os
from types import SimpleNamespace

import numpy as np

import quickbeam.watcher as W


class _FakeAsyncLines:
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._lines:
            return self._lines.pop(0)
        raise StopAsyncIteration


class _FakeProc:
    """Stand-in for the `fangorn subscribe` subprocess: streams the given live-change
    lines, then EOFs."""
    def __init__(self, stdout_lines):
        self.stdout = _FakeAsyncLines(stdout_lines)
        self.stderr = _FakeAsyncLines([])
        self.pid = 1
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = 0


class _FakeEngine:
    def __init__(self):
        self.seen = []

    def embed(self, texts, batch_size=16):
        self.seen.extend(texts)
        return [np.array([9.0, 9.0, 9.0, 9.0]) for _ in texts]


class _FakeQdrant:
    def __init__(self):
        self.uploaded = []
        self.deletes = []

    def upload_points(self, collection_name, points, batch_size=256):
        self.uploaded.extend(points)

    def delete(self, collection_name, points_selector, wait=True):
        self.deletes.append(points_selector)


def _v(cid, schema, payload):
    return {"kind": "vertex", "cid": cid, "schemaId": schema, "payload": payload}


def _run():
    # Seed stream: an Asset with one Transfer (edge), plus three edgeless PriceBars.
    seed_items = [
        {"kind": "head", "owner": "o", "namespace": "n", "head": "0xabc"},
        _v("AAPL", "Asset", {"name": "Apple", "text": "Apple stock"}),
        _v("T1", "Transfer", {"name": "xfer1", "text": "big buy"}),
        {"kind": "edge", "sourceCid": "AAPL", "relation": "hasTransfer", "targetCid": "T1"},
        _v("B1", "PriceBar", {"symbol": "AAPL", "ts": 1, "close": 100}),
        _v("B2", "PriceBar", {"symbol": "AAPL", "ts": 2, "close": 101}),
        _v("B3", "PriceBar", {"symbol": "AAPL", "ts": 3, "close": 102}),
    ]

    # One live change: add a new bar, no removals. Must NOT tombstone the seeded bars.
    live_change = json.dumps({
        "blockNumber": 5, "newRoot": "0xdef", "commitCid": "cid5",
        "addedVertices": [{"cid": "B4", "schemaId": "PriceBar",
                           "payload": {"symbol": "AAPL", "ts": 4, "close": 103}}],
        "addedEdges": [], "removedVertexCids": [], "removedEdges": [],
    }).encode() + b"\n"

    def fake_iter(binn, owner, ns, timeout):
        async def gen():
            for it in seed_items:
                yield it
        return gen()

    async def fake_exec(*a, **k):
        return _FakeProc([live_change])

    W._iter_read_ndjson = fake_iter
    asyncio.create_subprocess_exec = fake_exec  # only the subscribe proc reaches this

    engine, qdrant = _FakeEngine(), _FakeQdrant()
    tmp = tempfile.mkdtemp()
    args = SimpleNamespace(
        collection="c", embed_batch=16, searchable_fields="auto",
        embedding_model="unknown", structured_types="PriceBar",
        max_depth=1, label_cap=50, node_cap=1000,
        checkpoint_file=os.path.join(tmp, "ck.json"),
        role_map_file=os.path.join(tmp, "roles.json"),
        cdn_dir=None, cdn_domain=None, cdn_config="domains.json",
        fangorn_bin="fangorn", poll_interval=1, seed_timeout=30,
    )
    role_map_ref = [{}]
    checkpoint = {"processed_track_ids": [], "sources": {}}
    snapshot = {"seeded": False, "vertices": {}, "edges": {}}

    asyncio.run(W._stream_source_once(
        args, qdrant, engine, role_map_ref, dim=4, truncate=False,
        checkpoint=checkpoint, owner="o", namespace="n", snapshot=snapshot))

    return engine, qdrant, snapshot


def test_streaming_seed_bypasses_structured_and_never_mass_tombstones():
    engine, qdrant, snapshot = _run()
    by_id = {p.payload["id"]: p for p in qdrant.uploaded}

    # 1. All four bars (3 seed + 1 live) rode the constant placeholder vector...
    for bar in ("B1", "B2", "B3", "B4"):
        assert by_id[bar].vector == [1.0, 0.0, 0.0, 0.0], bar
    # ...and NONE of them ever touched the embed model.
    assert not any("100" in t or "101" in t or "102" in t or "103" in t for t in engine.seen)
    assert all("Apple" in t or "xfer" in t or "big buy" in t for t in engine.seen)

    # 2. The embedded graph stayed resident and folded the Transfer into the Asset.
    assert snapshot["seeded"] is True
    assert set(snapshot["vertices"]) == {"AAPL", "T1"}      # bars NOT resident
    assert by_id["AAPL"].payload["fields"].get("transfers") == ["xfer1"]

    # 3. THE regression guard: no tombstone delete fired despite the live change —
    #    structured rows never entered the diff, so nothing was wrongly "removed".
    assert qdrant.deletes == []


if __name__ == "__main__":
    test_streaming_seed_bypasses_structured_and_never_mass_tombstones()
    print("ok")
