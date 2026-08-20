"""The watch list is (app, owner, namespace) triples, and the CDN domain derived from
one must match the registry worker's `domainFor()` byte for byte.

That cross-language rule is the silent-failure mode of this whole feature: the watcher
NAMES the CDN directory and the worker names it BACK to filter a view's catalog, so a
drift returns an empty catalog with no error anywhere. The last test re-derives the
worker's version from its actual source rather than a copied constant, so editing one
side without the other fails here instead of in production.
"""
import json
import re
import sys
import types
from pathlib import Path
from unittest.mock import patch

# Everything under test here is pure string/parsing logic, but importing the watcher
# drags in the whole embedding stack. Stub the leaves so this file runs on a plain
# checkout (`pip install -e .` with no [cpu] extra) instead of only inside the image.
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

from quickbeam.watcher import _domain_for, _fetch_sources, _sort_key, _wild  # noqa: E402

OWNER = "0x7a7849231cF7Ab1EA003BcF0063CB89704D7Cce9"
WORKER = Path(__file__).resolve().parents[2] / "webworker/quickbeam-registry/src/index.js"


def _watchlist(payload, default_app=None):
    """Run _fetch_sources against a canned body, without a real HTTP call."""
    class _Resp:
        def read(self): return json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch("urllib.request.urlopen", return_value=_Resp()):
        return _fetch_sources("https://reg.test/watchlist", default_app)


def test_wild():
    assert _wild("*") is None and _wild("") is None and _wild("  ") is None
    assert _wild(" ns ") == "ns"


def test_objects_carry_their_own_app():
    got = _watchlist({"sources": [
        {"app": "a.1", "owner": OWNER, "namespace": "media"},
        {"app": "b.1", "owner": OWNER, "namespace": "media"},
    ]})
    # Same publisher:subspace in two apps must stay two sources, or one goes unwatched.
    assert got == {("a.1", OWNER, "media"), ("b.1", OWNER, "media")}


def test_wildcard_sides_become_none():
    assert _watchlist([{"app": "a.1", "owner": "*", "namespace": "*"}]) == {("a.1", None, None)}
    assert _watchlist([{"app": "a.1", "owner": OWNER}]) == {("a.1", OWNER, None)}


def test_string_forms():
    assert _watchlist([f"a.1:{OWNER}:media"]) == {("a.1", OWNER, "media")}
    # The older two-part form still parses, taking the --app default.
    assert _watchlist([f"{OWNER}:media"], "fallback.1") == {("fallback.1", OWNER, "media")}


def test_entry_without_any_app_is_dropped():
    # No app on the entry and no --app to fall back on: watching the wrong app would be
    # worse than watching nothing.
    assert _watchlist([{"owner": OWNER, "namespace": "media"}]) == set()
    assert _watchlist([{"owner": OWNER, "namespace": "media"}], "d.1") == {("d.1", OWNER, "media")}


def test_sort_key_tolerates_wildcards():
    # None cannot be compared against str, so main()'s sorted() needs this.
    assert sorted({("a", None, None), ("a", OWNER, "ns")}, key=_sort_key) \
        == [("a", None, None), ("a", OWNER, "ns")]


def test_app_reaches_the_qdrant_payload():
    """meta.app must survive projection AND the upsert.

    _embed_and_upload builds its `meta` payload as an explicit dict literal, so a key
    added to project_source's meta is silently dropped there unless added twice. That is
    exactly how this shipped broken once: every scoped search matched nothing because
    meta.app was never written, while the projection looked correct.
    """
    from quickbeam.ingest.graph.projection import project_source

    args = type("A", (), {"max_depth": 1, "label_cap": 5, "node_cap": 10})()
    contents = {"vertices": [{"cid": "bafy1", "schemaId": "Doc",
                              "payload": {"title": "hello"}}], "edges": []}
    profiles = [{"name": "doc", "root_type": "Doc"}]
    records = project_source("a.1", OWNER, "media", contents, profiles, args)
    assert records and records[0]["meta"]["app"] == "a.1", "projection dropped the app"

    embed_src = (Path(__file__).resolve().parents[1] / "quickbeam/ingest/embed.py").read_text()
    payload = re.search(r'"meta":\s*\{(.+?)\},', embed_src, re.S)
    assert payload, "the upsert meta payload literal moved — re-point this guard"
    assert '"app"' in payload.group(1), \
        "embed.py's meta payload does not write app; every app-scoped filter will miss"


APP_ID = "0x" + "a3f19b2c" + "0" * 56  # shape of toAppId(): 0x + 64 hex


def test_domain_matches_the_worker():
    """_domain_for(app, owner, namespace) == the worker's domainFor(app, owner, ns)."""
    src = WORKER.read_text()
    body = re.search(r"function domainFor\([^)]*\)\s*\{\s*return\s*`([^`]+)`", src)
    assert body, "domainFor() not found in the worker — did it get renamed?"
    assert body.group(1) == "${appSlug(app)}-${slug(owner.slice(2, 10))}-${slug(namespace)}", \
        "the worker's domain shape changed; update _domain_for in watcher.py to match"

    slug = re.search(r"function slug\(text\)\s*\{\s*return\s*(.+?);\s*\n\}", src, re.S)
    assert slug, "slug() not found in the worker"
    # Same character class, same trim, same lowercase, same 'x' fallback as _slug.
    assert "[^A-Za-z0-9_-]+" in slug.group(1) and "'x'" in slug.group(1)

    # An app id is sliced like the owner; a bare name is slugged whole. Both sides
    # must agree, so assert the worker's appSlug() branches the same way.
    app_slug = re.search(r"function appSlug\(app\)\s*\{\s*return\s*(.+?);\s*\n\}", src, re.S)
    assert app_slug, "appSlug() not found in the worker"
    assert "slice(2, 10)" in app_slug.group(1), "worker no longer slices the app id"

    assert _domain_for(APP_ID, OWNER, "media") == "a3f19b2c-7a784923-media"
    assert _domain_for("sond3r.test.1", OWNER, "media") == "sond3r-test-1-7a784923-media"


def test_worker_canonicalises_app_to_an_id():
    """The site can only ever send an app ID — `StateCommitted` carries the id and has no
    on-chain preimage, so an app the site has no name for still has to be expressible.
    The worker must therefore accept a 0x id, not just a name."""
    src = WORKER.read_text()
    # Names are hashed with the SDK's rule: keccak256(toHex(name)).
    assert "keccak256(toHex(value))" in src, \
        "worker no longer hashes an app name the way the SDK's appId() does"
    # And the POST gate must test the canonical id, not isSafeName (which caps at 48
    # chars and so rejects every 66-char app id the website sends).
    assert "!APP_ID_RE.test(s.app)" in src, \
        "worker validates app with something other than the canonical id test"

