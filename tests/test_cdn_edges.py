"""
Tests for the CDN relational-axis delivery (cdn.write_edges / _coerce_edges).

`cdn edges` installs a domain's linkset as `cdn/<domain>/edges.json`, served at
`/domains/{name}/edges`, so a pull-client (mcp_server.py) can walk the graph
offline. These tests cover the write + catalog sync + edge coercion.

Run:  ./venv/bin/python -m pytest tests/test_cdn_edges.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickbeam import cdn


def _baked_cdn(tmp_path: Path) -> Path:
    """A minimal baked CDN: one domain with a manifest + a catalog entry."""
    dom = tmp_path / "robinhood"
    dom.mkdir()
    (dom / "manifest.json").write_text(json.dumps({"name": "robinhood", "count": 3}))
    (tmp_path / "catalog.json").write_text(json.dumps(
        {"domains": [{"name": "robinhood", "count": 3}]}))
    return tmp_path


def test_coerce_edges_filters_and_projects():
    raw = [
        {"rel": "hasTransfer", "from": "rh:asset:NVDA", "to": "rh:xfer:1",
         "fromType": "Asset", "toType": "Transfer", "extra": "dropped"},
        {"rel": "hasNews", "from": "rh:asset:GME"},        # no `to` — dropped
        {"from": "a", "to": "b"},                          # no `rel` — dropped
        "not-a-dict",                                      # dropped
    ]
    out = cdn._coerce_edges(raw)
    assert len(out) == 1
    assert out[0] == {"rel": "hasTransfer", "from": "rh:asset:NVDA", "to": "rh:xfer:1",
                      "fromType": "Asset", "toType": "Transfer"}
    # Also accepts the wrapped {edges:[...]} form.
    assert cdn._coerce_edges({"edges": raw}) == out


def test_write_edges_creates_file_and_syncs_catalog(tmp_path):
    cdn_dir = _baked_cdn(tmp_path)
    edges = [
        {"rel": "hasTransfer", "from": "rh:asset:NVDA", "to": "rh:xfer:1",
         "fromType": "Asset", "toType": "Transfer"},
        {"rel": "hasNews", "from": "rh:asset:NVDA", "to": "rh:news:1",
         "fromType": "Asset", "toType": "NewsSentiment"},
    ]
    summary = cdn.write_edges(str(cdn_dir), "robinhood", edges)
    assert summary == {"domain": "robinhood", "count": 2,
                       "relations": ["hasNews", "hasTransfer"]}

    payload = json.loads((cdn_dir / "robinhood" / "edges.json").read_text())
    assert payload["count"] == 2
    assert payload["relations"] == ["hasNews", "hasTransfer"]
    assert payload["edges"][0]["from"] == "rh:asset:NVDA"

    # Catalog reflects the relational axis.
    entry = json.loads((cdn_dir / "catalog.json").read_text())["domains"][0]
    assert entry["edge_count"] == 2
    assert entry["relations"] == ["hasNews", "hasTransfer"]


def test_write_edges_requires_baked_domain(tmp_path):
    with pytest.raises(SystemExit):
        cdn.write_edges(str(tmp_path), "never-baked", [])


def test_append_edges_merges_and_dedups(tmp_path):
    """The live watcher path: each cycle merges new edges into edges.json, deduping
    by (rel, from, to), never re-shipping the whole linkset."""
    cdn_dir = _baked_cdn(tmp_path)
    e1 = {"rel": "hasTransfer", "from": "rh:asset:NVDA", "to": "rh:xfer:1",
          "fromType": "Asset", "toType": "Transfer"}
    e2 = {"rel": "hasTransfer", "from": "rh:asset:NVDA", "to": "rh:xfer:2",
          "fromType": "Asset", "toType": "Transfer"}
    e3 = {"rel": "hasNews", "from": "rh:asset:GME", "to": "rh:news:1",
          "fromType": "Asset", "toType": "NewsSentiment"}

    # Cycle 1: two new edges.
    s1 = cdn.append_edges(str(cdn_dir), "robinhood", [e1, e2])
    assert s1["added"] == 2 and s1["count"] == 2

    # Cycle 2: e2 is a repeat (deduped), e3 is new → only +1, total 3.
    s2 = cdn.append_edges(str(cdn_dir), "robinhood", [e2, e3])
    assert s2["added"] == 1 and s2["count"] == 3
    assert set(s2["relations"]) == {"hasTransfer", "hasNews"}

    # Cycle 3: nothing new → no rewrite (None), idempotent.
    assert cdn.append_edges(str(cdn_dir), "robinhood", [e1, e2, e3]) is None

    payload = json.loads((cdn_dir / "robinhood" / "edges.json").read_text())
    assert payload["count"] == 3
    keys = {(e["rel"], e["from"], e["to"]) for e in payload["edges"]}
    assert keys == {("hasTransfer", "rh:asset:NVDA", "rh:xfer:1"),
                    ("hasTransfer", "rh:asset:NVDA", "rh:xfer:2"),
                    ("hasNews", "rh:asset:GME", "rh:news:1")}
