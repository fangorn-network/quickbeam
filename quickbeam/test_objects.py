"""
Conformance + unit tests for the git-native object model (objects.py).

Run standalone:      python test_objects.py
Or under pytest:     pytest quickbeam/test_objects.py

The headline check is `test_golden_fixture_parity`: the Python canonicalizer must
reproduce the shared golden bytes (`tests/fixtures/commit.canonical.txt`) that the
TypeScript canonicalizer produced — that byte-for-byte agreement is what lets the
two languages share object identity (see fangorn/docs/objects/README.md).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from objects import (  # noqa: E402
    BlobRef,
    blob_cids,
    blob_refs,
    canonicalize,
    collect_removed_point_ids,
    diff_trees,
    first_parent,
    is_commit,
    plan_delta,
    resolve_embed,
)

_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures"
)


# ---------------------------------------------------------------------------
# S0 golden fixture — cross-language canonical parity
# ---------------------------------------------------------------------------
def test_golden_fixture_parity():
    with open(os.path.join(_FIXTURES, "commit.fixture.json"), encoding="utf-8") as f:
        commit = json.load(f)
    with open(os.path.join(_FIXTURES, "commit.canonical.txt"), encoding="utf-8") as f:
        expected = f.read()
    assert canonicalize(commit) == expected


def test_canonicalize_sorts_and_drops_none():
    assert canonicalize({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    # None-valued keys are dropped so optional fields never perturb the hash.
    assert canonicalize({"a": 1, "skip": None}) == '{"a":1}'
    # non-ASCII stays literal (UTF-8), never \uXXXX-escaped.
    assert canonicalize({"m": "café"}) == '{"m":"café"}'
    # nested sorting is recursive.
    assert canonicalize({"z": {"y": 1, "x": 2}}) == '{"z":{"x":2,"y":1}}'


def test_canonicalize_rejects_nonfinite():
    for bad in (float("inf"), float("-inf"), float("nan")):
        try:
            canonicalize({"v": bad})
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


# ---------------------------------------------------------------------------
# is_commit / first_parent / resolve_embed
# ---------------------------------------------------------------------------
def _commit(**over):
    base = {
        "kind": "commit", "parents": [], "tree": "bafyT", "root": "0x1",
        "schemaId": "0x2", "author": "0x3", "message": "m", "timestamp": 1,
    }
    base.update(over)
    return base


def test_is_commit():
    assert is_commit(_commit())
    assert not is_commit({"kind": "bundle"})
    assert not is_commit({**_commit(), "kind": "tree"})
    assert not is_commit(None)
    # lenient: an unknown extra field must not reject an otherwise-valid commit.
    assert is_commit({**_commit(), "futureField": 1})


def test_first_parent():
    assert first_parent(_commit(parents=[])) is None
    assert first_parent(_commit(parents=["a", "b"])) == "a"


def test_resolve_embed_prefers_commit_then_falls_back():
    c = _commit(embed={"model": "M", "dim": 128, "distance": "Dot"})
    assert resolve_embed(c, "fallbackM", 256) == {"model": "M", "dim": 128, "distance": "Dot"}
    # missing embed -> all fallbacks (and default distance).
    assert resolve_embed(_commit(), "fallbackM", 256) == {
        "model": "fallbackM", "dim": 256, "distance": "Cosine"}
    # partial embed -> per-field fallback.
    assert resolve_embed(_commit(embed={"dim": 64}), "fallbackM", 256)["model"] == "fallbackM"
    assert resolve_embed(None, "fallbackM", 256)["dim"] == 256


# ---------------------------------------------------------------------------
# blob extraction across manifest kinds
# ---------------------------------------------------------------------------
def test_blob_refs_record_set():
    m = {"kind": "record-set", "entries": [
        {"fields": {"dataCid": "ipfs://car1/0", "contentId": "sha-a"}},
        {"fields": {"dataCid": "ipfs://car1/1", "contentId": "sha-b"}},
        {"fields": {"other": 1}},  # no dataCid -> skipped
    ]}
    assert blob_refs(m) == [
        BlobRef("sha-a", "ipfs://car1/0"),
        BlobRef("sha-b", "ipfs://car1/1"),
    ]
    assert blob_cids(m) == ["sha-a", "sha-b"]


def test_blob_refs_bundle_including_legacy_edge_chunk():
    m = {"kind": "bundle",
         "nodeChunks": [{"dataCid": "ipfs://c/0", "contentId": "n0"}],
         "edgeChunk": {"dataCid": "ipfs://c/1", "contentId": "e0"}}
    assert blob_cids(m) == ["n0", "e0"]
    m2 = {"kind": "bundle",
          "nodeChunks": [{"dataCid": "ipfs://c/0", "contentId": "n0"}],
          "edgeChunks": [{"dataCid": "ipfs://c/1", "contentId": "e0"},
                         {"dataCid": "ipfs://c/2", "contentId": "e1"}]}
    assert blob_cids(m2) == ["n0", "e0", "e1"]


def test_blob_refs_view_and_linkset():
    assert blob_cids({"kind": "view", "viewChunk": {"dataCid": "u", "contentId": "v"}}) == ["v"]
    assert blob_cids({"kind": "linkset", "linkChunks": [
        {"dataCid": "u0", "contentId": "l0"}, {"dataCid": "u1", "contentId": "l1"}]}) == ["l0", "l1"]


def test_blob_refs_falls_back_to_uri_when_no_content_id():
    # pre-contentId manifest: identity coarsely equals the uri (old behavior).
    m = {"kind": "record-set", "entries": [{"fields": {"dataCid": "ipfs://old/0"}}]}
    assert blob_cids(m) == ["ipfs://old/0"]


# ---------------------------------------------------------------------------
# diff — the heart of incremental builds + delete propagation
# ---------------------------------------------------------------------------
def _rs(*pairs):
    return {"kind": "record-set",
            "entries": [{"fields": {"dataCid": uri, "contentId": cid}} for cid, uri in pairs]}


def test_diff_root_commit_adds_everything():
    child = _rs(("a", "u0"), ("b", "u1"))
    d = diff_trees(None, child)
    assert d.added == ["a", "b"]
    assert d.removed == []


def test_diff_add_and_remove():
    parent = _rs(("a", "u0"), ("b", "u1"))
    child = _rs(("b", "u1"), ("c", "u2"))  # drop a, keep b, add c
    d = diff_trees(parent, child)
    assert d.added == ["c"]
    assert d.removed == ["a"]


def test_diff_keys_on_content_id_not_uri():
    # Structural sharing: byte-identical chunks keep their contentId even when the
    # per-CAR retrieval uri changes across commits, so they diff to *no change*.
    parent = _rs(("a", "ipfs://car1/0"), ("b", "ipfs://car1/1"))
    child = _rs(("a", "ipfs://car2/0"), ("b", "ipfs://car2/1"))  # same ids, new uris
    d = diff_trees(parent, child)
    assert d.added == []
    assert d.removed == []


def test_plan_delta_returns_uris_not_ids():
    # child adds c (new uri), keeps b (uri changed but same id), drops a.
    parent = _rs(("a", "ipfs://car1/0"), ("b", "ipfs://car1/1"))
    child = _rs(("b", "ipfs://car2/0"), ("c", "ipfs://car2/1"))
    plan = plan_delta(child, parent)
    # b is byte-identical (same content id) -> not re-fetched despite the new uri.
    assert plan.added_uris == ["ipfs://car2/1"]        # only c
    assert plan.removed_uris == ["ipfs://car1/0"]      # only a, at its PARENT uri
    assert not plan.is_empty()


def test_plan_delta_root_commit():
    child = _rs(("a", "u0"), ("b", "u1"))
    plan = plan_delta(child, None)
    assert plan.added_uris == ["u0", "u1"]
    assert plan.removed_uris == []


def test_plan_delta_noop_when_only_uris_change():
    parent = _rs(("a", "ipfs://car1/0"))
    child = _rs(("a", "ipfs://car2/0"))
    assert plan_delta(child, parent).is_empty()


def test_collect_removed_point_ids_fans_out_and_dedups():
    blobs = {
        "u0": [{"id": "a"}, {"id": "b"}],
        "u1": [{"id": "b"}, {"id": "c"}, "junk-not-a-dict"],
    }
    ids = collect_removed_point_ids(["u0", "u1"], blobs, lambda r: r["id"])
    # order preserved, duplicate 'b' collapsed, non-dict skipped.
    assert ids == ["a", "b", "c"]


def test_collect_removed_point_ids_missing_blob():
    ids = collect_removed_point_ids(["missing"], {}, lambda r: r["id"])
    assert ids == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
