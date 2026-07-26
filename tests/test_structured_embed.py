"""_embed_and_upload's structured-only skip: entityTypes named in --structured-types
are upserted with a constant placeholder vector and NEVER passed through the embed model
(the GPU bottleneck), while normal records still embed. No GPU/Qdrant: fakes capture what
the model saw and what got uploaded. Run: `python -m pytest tests/test_structured_embed.py`."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np

from quickbeam.ingest.embed import _embed_and_upload


class _FakeEngine:
    def __init__(self):
        self.seen = []

    def embed(self, texts, batch_size=16):
        self.seen.extend(texts)
        return [np.array([9.0, 9.0, 9.0, 9.0]) for _ in texts]


class _FakeQdrant:
    def __init__(self):
        self.uploaded = []

    def upload_points(self, collection_name, points, batch_size=256):
        self.uploaded.extend(points)


def _rec(track_id, entity_type, text=None):
    fields = {"entityType": entity_type, "symbol": track_id}
    if text is not None:
        fields["text"] = text
    return {"track_id": track_id, "entity_type": entity_type, "fields": fields,
            "meta": {"owner": "o", "namespace": "n", "sourceCid": "c"}}


def test_structured_rows_skip_the_model_but_still_upload():
    engine, qdrant = _FakeEngine(), _FakeQdrant()
    args = SimpleNamespace(collection="c", embed_batch=16, searchable_fields="auto",
                           embedding_model="unknown", structured_types="PriceBar")
    records = [_rec("SPY", "PriceBar"), _rec("AAPL", "Asset", text="Apple stock"),
               _rec("NVDA", "PriceBar")]
    ck = {"processed_track_ids": []}
    asyncio.run(_embed_and_upload(args, qdrant, engine, records, {"text": ["text"]},
                                  dim=4, truncate=False, checkpoint=ck))

    # The model saw ONLY the one non-structured record's text — 780k bars never touch it.
    assert engine.seen == ["search_document: Title: . Tags: . Apple stock"]
    # All three uploaded, order preserved; bars carry the constant placeholder vector.
    by_id = {p.payload["id"]: p.vector for p in qdrant.uploaded}
    assert by_id["SPY"] == [1.0, 0.0, 0.0, 0.0]
    assert by_id["NVDA"] == [1.0, 0.0, 0.0, 0.0]
    assert by_id["AAPL"] == [9.0, 9.0, 9.0, 9.0]
    assert ck["processed_track_ids"] == ["SPY", "AAPL", "NVDA"]


def test_no_structured_types_is_unchanged_behavior():
    engine, qdrant = _FakeEngine(), _FakeQdrant()
    args = SimpleNamespace(collection="c", embed_batch=16, searchable_fields="auto",
                           embedding_model="unknown", structured_types="")
    records = [_rec("AAPL", "Asset", text="a"), _rec("MSFT", "Asset", text="b")]
    asyncio.run(_embed_and_upload(args, qdrant, engine, records, {"text": ["text"]},
                                  dim=4, truncate=False, checkpoint={"processed_track_ids": []}))
    assert len(engine.seen) == 2 and all(p.vector == [9.0, 9.0, 9.0, 9.0] for p in qdrant.uploaded)


if __name__ == "__main__":
    test_structured_rows_skip_the_model_but_still_upload()
    test_no_structured_types_is_unchanged_behavior()
    print("ok")
