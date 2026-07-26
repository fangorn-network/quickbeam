"""The publish leg's batch writer: staged volumes → one fangorn batch file.

`_write_staged_batch` emits the JSON by hand (record-by-record, so a large
namespace never has to be resident) — so the thing worth checking is that the
hand-rolled brackets/commas still parse and that every staged row lands.
"""
import json

from quickbeam.ingest.scrapers.harness import _write_staged_batch


class _Src:
    name = "fake"
    stems = {"Asset": "assets", "PriceBar": "pricebars"}
    edges_stem = "edges"


def _stage(d, stem, rows):
    (d / f"volume_1_{stem}.json").write_text(json.dumps(rows))


def test_batch_roundtrips(tmp_path):
    _stage(tmp_path, "assets", [{"name": "a:1", "fields": {"sym": "AAPL"}}])
    _stage(tmp_path, "pricebars", [
        {"name": f"b:{i}", "fields": {"close": i}} for i in range(3)])
    _stage(tmp_path, "edges", [
        {"rel": "hasBar", "from": "a:1", "to": "b:0"},
        {"rel": "hasBar", "from": "a:1", "to": "b:1"},
    ])

    out = tmp_path / "batch.json"
    assert _write_staged_batch(_Src(), str(tmp_path), 1, str(out)) == (4, 2)

    batch = json.loads(out.read_text())
    assert {v["id"] for v in batch["vertices"]} == {"a:1", "b:0", "b:1", "b:2"}
    assert {v["tag"] for v in batch["vertices"]} == {"Asset", "PriceBar"}
    assert batch["vertices"][0] == {
        "id": "a:1", "tag": "Asset", "payload": {"sym": "AAPL"}}
    assert batch["edges"] == [
        {"rel": "hasBar", "from": "a:1", "to": "b:0"},
        {"rel": "hasBar", "from": "a:1", "to": "b:1"},
    ]


def test_empty_dir_is_valid_empty_batch(tmp_path):
    out = tmp_path / "batch.json"
    assert _write_staged_batch(_Src(), str(tmp_path), 1, str(out)) == (0, 0)
    assert json.loads(out.read_text()) == {"vertices": [], "edges": []}
