"""A wildcard source's CDN domain, which is chosen per change instead of per task.

A pinned source names its domain once, at task start. A wildcard one (`*:*`, `0x..:*`,
`*:ns`) learns its pairs only as commits arrive, so `_pair_cdn_args` derives the domain
from the change itself — and bakes it the first time a pair is seen, because
`append_domain` extends a manifest and has nothing to extend without one.

The names here are the contract with `domainFor()` in the registry worker: byte drift
between the two returns an empty catalog with no error on either side.
"""
import argparse
import os

from quickbeam import watcher

OWNER = "0x7a7849231cF7Ab1EA003BcF0063CB89704D7Cce9"
OTHER = "0x8ce65916C8b83b4c62dAd51b462643A1ae59899b"


def _args(cdn_dir, cdn_domain=None, app="sond3r.test.0"):
    return argparse.Namespace(cdn_dir=cdn_dir, cdn_domain=cdn_domain, app=app)


def _baked(monkeypatch) -> list:
    """Record bakes instead of running one — a real bake needs Qdrant."""
    calls = []
    monkeypatch.setattr(watcher, "_bake_initial",
                        lambda args, qdrant, owner, ns: calls.append((args.cdn_domain, owner, ns)))
    return calls


def test_wildcard_pair_gets_its_own_domain(tmp_path, monkeypatch):
    calls = _baked(monkeypatch)
    task = _args(str(tmp_path))

    a = watcher._pair_cdn_args(task, None, OWNER, "media")
    b = watcher._pair_cdn_args(task, None, OTHER, "media")

    assert a.cdn_domain == "sond3r-test-0-7a784923-media"
    assert a.cdn_domain == watcher._domain_for(task.app, OWNER, "media")
    # Same subspace name, different publisher: two domains, or their shards intermix.
    assert b.cdn_domain == "sond3r-test-0-8ce65916-media"
    # The task's own args must stay wildcard-shaped, or the first pair seen would
    # capture every later pair's shards.
    assert task.cdn_domain is None
    assert [c[0] for c in calls] == [a.cdn_domain, b.cdn_domain]


def test_bake_only_when_the_domain_has_no_manifest(tmp_path, monkeypatch):
    calls = _baked(monkeypatch)
    task = _args(str(tmp_path))
    domain = watcher._domain_for(task.app, OWNER, "media")
    os.makedirs(tmp_path / domain)
    (tmp_path / domain / "manifest.json").write_text("{}")

    scoped = watcher._pair_cdn_args(task, None, OWNER, "media")

    assert scoped.cdn_domain == domain
    assert calls == []  # already delivered by an earlier connection


def test_pinned_and_delivery_off_are_untouched(tmp_path, monkeypatch):
    calls = _baked(monkeypatch)
    pinned = _args(str(tmp_path), cdn_domain="pinned-domain")
    off = _args(None)

    # Same object back, not a copy: a pinned task already baked its one domain.
    assert watcher._pair_cdn_args(pinned, None, OWNER, "media") is pinned
    assert watcher._pair_cdn_args(off, None, OWNER, "media") is off
    assert calls == []
