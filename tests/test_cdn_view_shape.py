"""The view shape a storefront gets with no registry worker in front of the CDN.

sond3r's src/search.js builds `${view}/cdn/catalog` and opens `${view}/stream`,
because the worker publishes a view as {registry}/q/{viewId} and rewrites those two
prefixes onto this origin. Pointed straight at a local `cdn serve`, nothing does that
rewrite — so build_app serves both shapes itself. These assert the mirror holds.

SSE is checked by inspecting the route table rather than opening a connection: the
/events generator only ends when the client disconnects, and TestClient blocks on it.
"""
import gzip
import json
import os

from fastapi.testclient import TestClient
from starlette.routing import Mount

from quickbeam.cdn import build_app

SHARD = "shard-0000.ndjson.gz"


def _cdn(tmp_path):
    os.makedirs(tmp_path / "sond3r")
    (tmp_path / "catalog.json").write_text(json.dumps({"domains": [{"name": "sond3r"}]}))
    (tmp_path / "sond3r" / "manifest.json").write_text(
        json.dumps({"shards": [{"file": SHARD}], "tombstones": []}))
    with gzip.open(tmp_path / "sond3r" / SHARD, "wt") as fh:
        fh.write('{"id":"a"}\n')
    return TestClient(build_app(str(tmp_path)))


def test_cdn_prefix_mirrors_bare_routes(tmp_path):
    """Every route the storefront pulls answers identically with and without /cdn."""
    client = _cdn(tmp_path)
    for route in ("/health", "/catalog", "/domains/sond3r/manifest",
                  f"/domains/sond3r/shards/{SHARD}"):
        bare, prefixed = client.get(route), client.get("/cdn" + route)
        assert bare.status_code == 200, route
        assert prefixed.status_code == 200, "/cdn" + route
        assert bare.content == prefixed.content, route


def test_cdn_prefix_keeps_shard_name_check(tmp_path):
    """The mount must not become a way around the shard-name guard."""
    assert _cdn(tmp_path).get("/cdn/domains/sond3r/shards/evil.txt").status_code == 400


def test_stream_aliases_events(tmp_path):
    """`/stream` is what search.js opens; it must reach the same handler as /events.

    build_app returns a wrapper whose routes are Mounts, so the real table lives one
    level down — under both mounts, which is the point.
    """
    # FastAPI's own /docs routes also carry `.app`, so match on Mount, not hasattr.
    mounts = {r.path: r.app for r in _cdn(tmp_path).app.routes if isinstance(r, Mount)}
    assert set(mounts) == {"/cdn", ""}, mounts.keys()
    for prefix, mounted in mounts.items():
        paths = {r.path for r in mounted.routes if hasattr(r, "endpoint")}
        assert "/stream" in paths, f"search.js opens {prefix}/stream"
        assert "/events" in paths, prefix
