"""Guards for query scoping — the filter that turns ONE shared collection into a
per-view slice.

The failure this pins is silent in both directions: an export that ignores its scope
hands every view the whole instance, and a scope parsed one part short filters on the
wrong field and returns nothing. Neither looks like an error at the HTTP layer.

The wire format is the registry worker's (`webworker/quickbeam-registry/src/index.js`,
`scopedParams`): `scope=APP:OWNER:NAMESPACE`, repeatable, an empty part meaning `*`.
"""
import argparse
import asyncio
import json

import pytest

from quickbeam import server

APP = "0x" + "ab" * 32
PUB = "0xowner"


class FakeRecord:
    def __init__(self, pid, app=APP, owner=PUB, namespace="media"):
        self.id = pid
        self.payload = {"id": f"rec:{pid}", "fields": {"title": f"t{pid}"}, "owner": owner,
                        "meta": {"app": app, "namespace": namespace, "sourceCid": "cid"}}
        self.vector = [0.1, 0.2, 0.3]


class FakeQdrant:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def scroll(self, collection_name, scroll_filter, limit, offset,
               with_payload, with_vectors):
        self.calls.append(scroll_filter)
        rows = [r for r in self.records if _matches(scroll_filter, r)]
        return rows, None


def _matches(flt, rec) -> bool:
    """Enough of Qdrant's filter semantics to tell a right filter from a wrong one."""
    if flt is None:
        return True
    if getattr(flt, "should", None):
        return any(_matches(sub, rec) for sub in flt.should)
    for cond in flt.must or []:
        key, want = cond.key, cond.match.value
        got = rec.payload["meta"].get(key.split(".", 1)[1]) if key.startswith("meta.") \
            else rec.payload.get(key)
        if got != want:
            return False
    return True


@pytest.fixture
def wired(monkeypatch):
    recs = [FakeRecord(0),
            FakeRecord(1, namespace="other"),
            FakeRecord(2, app="0x" + "cd" * 32),
            FakeRecord(3, owner="0xsomeoneelse")]
    fake = FakeQdrant(recs)
    monkeypatch.setattr(server, "qdrant_client", fake)
    monkeypatch.setattr(server, "cfg", argparse.Namespace(collection="test"))
    return fake


def _export(**kw):
    kw = {"limit": 0, "offset": 0, "app": None, "owner": None, "namespace": None,
          "scope": None, **kw}

    async def _drain():
        resp = await server.bundle_export(**kw)
        # StreamingResponse adapts the sync generator, so the body is async either way.
        return [json.loads(line) async for line in resp.body_iterator]

    return asyncio.run(_drain())


def test_scope_parses_the_worker_triple():
    assert server._parse_scope([f"{APP}:{PUB}:media"]) == [(APP, PUB, "media")]
    # A `*` source: empty parts stay unconstrained rather than matching "".
    assert server._parse_scope([f"{APP}::"]) == [(APP, None, None)]
    # Pre-app callers (and --source) still speak OWNER:NAMESPACE.
    assert server._parse_scope([f"{PUB}:media"]) == [(None, PUB, "media")]


def test_export_honours_the_view_scope(wired):
    """The reported bug: /bundle/export understood only `owner`, so the worker's
    injected scope was ignored and every view downloaded the whole instance."""
    rows = _export(scope=[f"{APP}:{PUB}:media"])
    assert [r["track_id"] for r in rows] == ["rec:0"]


def test_export_unscoped_is_the_whole_collection(wired):
    assert len(_export()) == 4


def test_export_ors_several_scopes(wired):
    rows = _export(scope=[f"{APP}:{PUB}:media", f"{APP}:{PUB}:other"])
    assert sorted(r["track_id"] for r in rows) == ["rec:0", "rec:1"]


def test_app_separates_a_shared_namespace_name(wired):
    """Two apps can hold the same publisher+subspace name. Dropping the app part
    would fold the other app's records into this view."""
    rows = _export(scope=[f"{APP}:{PUB}:media"])
    assert all(r["meta"]["app"] == APP for r in rows)


def test_text_search_filters_on_the_same_triple(monkeypatch):
    index = [{"id": "0", "app": APP, "owner": PUB, "namespace": "media", "fields": {},
              "_sub_l": "", "_title_l": "hello", "_tags_l": "", "_sort": ("", "hello")},
             {"id": "1", "app": "0x" + "cd" * 32, "owner": PUB, "namespace": "media",
              "fields": {}, "_sub_l": "", "_title_l": "hello", "_tags_l": "",
              "_sort": ("", "hello")}]
    monkeypatch.setattr(server, "_text_index", index)
    out = server._search_text_sync("hello", 10, scope=[f"{APP}:{PUB}:media"])
    assert [r["id"] for r in out] == ["0"]
