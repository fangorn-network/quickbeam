"""The checkpoint must never claim records the upload did not land.

`vertex_cids` is the set the NEXT diff subtracts to decide what is new. Persist it before
`_embed_and_upload` and a transient Qdrant failure becomes permanent silent loss: the
records read as already-held, `new_records` is empty forever, the CDN domain bakes zero
shards, and the seed log says "no new record(s) embedded" while the collection is empty.

That is not a hypothetical — a full disk on the shared instance did exactly this, and a
fresh container with a full re-seed could not recover it. These tests pin the ordering.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from unittest.mock import patch

# Importing the watcher drags in the embedding stack; stub the leaves so this runs on a
# plain checkout, exactly as tests/test_watchlist.py does.
for name in ("fastembed", "qdrant_client", "tqdm", "numpy"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)
sys.modules.setdefault("qdrant_client.models", types.ModuleType("qdrant_client.models"))
for mod, attr in (("qdrant_client", "QdrantClient"), ("qdrant_client", "models"),
                  ("fastembed", "TextEmbedding"), ("tqdm", "tqdm")):
    if not hasattr(sys.modules[mod], attr):
        setattr(sys.modules[mod], attr,
                sys.modules["qdrant_client.models"] if attr == "models" else object)

from quickbeam.ingest.checkpoint import _load_checkpoint  # noqa: E402
from quickbeam.watcher import _ingest_contents  # noqa: E402

OWNER = "0x8ce65916c8b83b4c62dad51b462643a1ae59899b"
APP = "0x7e1497af379dfd6843ca25e80f124cd014093cdde06e67facbcc9eeecec6affe"
KEY = f"{APP}:{OWNER}:media"

CONTENTS = {
    "vertices": [
        {"cid": "bafy1", "schemaId": "video", "payload": {"name": "one.mp4"}},
        {"cid": "bafy2", "schemaId": "video", "payload": {"name": "two.mp4"}},
    ],
    "edges": [],
}


class _Args:
    """Only the attributes _ingest_contents actually reaches for."""
    app = APP
    collection = "fangorn"
    root_profile: list = []
    profiles_file = None
    max_depth = 1
    label_cap = 5
    node_cap = 10
    searchable_fields = "auto"
    embed_batch = 16

    def __init__(self, tmp):
        self.checkpoint_file = os.path.join(tmp, "checkpoint.json")
        self.role_map_file = os.path.join(tmp, "role_map.json")


class _Qdrant:
    """Records deletes; upserts never reach here (we stub _embed_and_upload)."""
    def __init__(self):
        self.deleted = []

    def delete(self, **kw):
        self.deleted.append(kw)


def _run(args, qdrant, checkpoint):
    return asyncio.run(_ingest_contents(
        args, qdrant, embed_engine=object(), role_map_ref=[{}], dim=256, truncate=True,
        checkpoint=checkpoint, owner=OWNER, namespace="media", contents=CONTENTS))


def _stored_cids(args):
    return _load_checkpoint(args.checkpoint_file).get("sources", {}).get(KEY, {}) \
        .get("vertex_cids", [])


def test_failed_upload_does_not_advance_the_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        args, qdrant = _Args(tmp), _Qdrant()
        checkpoint = {"processed_track_ids": [], "sources": {}}

        async def boom(*a, **k):
            raise RuntimeError("Unexpected Response: 500 (Internal Server Error)")

        with patch("quickbeam.watcher._embed_and_upload", boom):
            try:
                _run(args, qdrant, checkpoint)
                raise AssertionError("the upload failure was swallowed")
            except RuntimeError as e:
                assert "500" in str(e)

        assert _stored_cids(args) == [], \
            "checkpoint recorded vertices the upload never landed — they can never retry"


def test_records_are_reoffered_and_only_then_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        args, qdrant = _Args(tmp), _Qdrant()
        checkpoint = {"processed_track_ids": [], "sources": {}}

        async def boom(*a, **k):
            raise RuntimeError("500")

        with patch("quickbeam.watcher._embed_and_upload", boom):
            try:
                _run(args, qdrant, checkpoint)
            except RuntimeError:
                pass

        # The retry — this is the step production could not reach.
        seen = []

        async def ok(args_, qdrant_, engine, records, *a, **k):
            seen.extend(r["track_id"] for r in records)

        with patch("quickbeam.watcher._embed_and_upload", ok):
            n = _run(args, qdrant, checkpoint)

        assert n == 2 and len(seen) == 2, f"records were not re-offered: {n}, {seen}"
        assert sorted(_stored_cids(args)) == sorted(seen), \
            "checkpoint should record exactly what was uploaded, once it succeeded"


def test_no_new_records_still_persists():
    """The empty path must persist too — it is what records tombstones applied above."""
    with tempfile.TemporaryDirectory() as tmp:
        args, qdrant = _Args(tmp), _Qdrant()
        checkpoint = {"processed_track_ids": [], "sources": {}}

        async def ok(*a, **k):
            pass

        with patch("quickbeam.watcher._embed_and_upload", ok):
            _run(args, qdrant, checkpoint)
        first = sorted(_stored_cids(args))
        assert first, "nothing recorded after a successful upload"

        # Same contents again: no new records, and the recorded set must survive.
        with patch("quickbeam.watcher._embed_and_upload", ok):
            n = _run(args, qdrant, checkpoint)
        assert n == 0
        assert sorted(_stored_cids(args)) == first


if __name__ == "__main__":
    for fn in (test_failed_upload_does_not_advance_the_checkpoint,
               test_records_are_reoffered_and_only_then_recorded,
               test_no_new_records_still_persists):
        fn()
        print("ok", fn.__name__)
