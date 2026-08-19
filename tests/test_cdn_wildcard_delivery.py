"""An app-level (`*:*`) watch must still ship CDN shards.

A wildcard source has no (owner, namespace) at task start, so it gets no `cdn_domain`.
That used to mean no delivery at all: the records embedded into Qdrant perfectly and the
view's `/cdn/catalog` stayed `domains: []` for ever — invisible unless you read the
served catalog rather than /search, and indistinguishable from "nothing published yet".

The pair IS known per change, so the domain is derived and baked on first sight. These
tests pin that, and pin the bake↔append handoff: a freshly baked snapshot already holds
the change's points, so appending them on top would ship every row twice.
"""
import os
import sys
import tempfile
import types
from unittest.mock import patch

# Importing the watcher drags in the embedding stack; stub the leaves so this runs on a
# plain checkout, exactly as tests/test_ingest_checkpoint.py does.
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

from quickbeam.watcher import _deliver_cdn, _domain_for, _source_args  # noqa: E402

OWNER = "0x8ce65916c8b83b4c62dad51b462643a1ae59899b"
APP = "0xda05f74b63a66ab992a3053c71f0866bcd44d2a43b0a13cc5dd9a19aa52e4805"
NS = "media"


class Args:
    """The handful of attrs the delivery path reads."""
    def __init__(self, cdn_dir):
        self.cdn_dir = cdn_dir
        self.cdn_domain = None      # what a wildcard source gets
        self.cdn_config = "domains.json"
        self.collection = "fangorn"
        self.app = APP
        self.embedding_model = "nomic-ai/nomic-embed-text-v1.5"
        self.role_map_file = "./db/role_map.json"   # _source_args scopes this per source


def _deliver(args, **kw):
    """One change delivered, with the CDN writes captured instead of performed."""
    calls = {"bake": [], "append": []}

    def bake(qdrant, collection, cdn_dir, domain, spec=None, config_path=None, model=None):
        calls["bake"].append((domain, spec))
        os.makedirs(os.path.join(cdn_dir, domain), exist_ok=True)
        open(os.path.join(cdn_dir, domain, "manifest.json"), "w").write("{}")
        return {"count": 3}

    def append(qdrant, collection, cdn_dir, domain, **kwargs):
        calls["append"].append((domain, kwargs))

    cdn = types.ModuleType("quickbeam.cdn")
    cdn.bake_domain, cdn.append_domain = bake, append
    cdn.append_tombstones = lambda *a, **k: None
    cdn.append_edges = lambda *a, **k: None
    with patch.dict(sys.modules, {"quickbeam.cdn": cdn}):
        _deliver_cdn(args, object(), kw.pop("total_new", 3), [], [],
                     owner=OWNER, namespace=NS, app=APP, **kw)
    return calls


def test_wildcard_change_bakes_its_own_domain():
    """The domain name must be the one the registry worker filters a view's catalog
    BACK by — derive it the same way a pinned source does, or the shards are written
    where nobody looks."""
    with tempfile.TemporaryDirectory() as tmp:
        calls = _deliver(Args(tmp))
        assert len(calls["bake"]) == 1, "a wildcard change must bake its pair's domain"
        domain, spec = calls["bake"][0]
        assert domain == _domain_for(APP, OWNER, NS), f"wrong domain name: {domain}"
        # Scoped to this pair — the collection holds every watched namespace, and a
        # bake-everything spec sweeps the rest of them into this domain.
        assert spec["filter"] == {"app": [APP], "owner": [OWNER], "namespace": [NS]}
        assert not calls["append"], "the snapshot already holds these points"


def test_second_change_appends_instead_of_rebaking():
    with tempfile.TemporaryDirectory() as tmp:
        args = Args(tmp)
        _deliver(args)                      # first change bakes
        calls = _deliver(args)              # second one must not
        assert not calls["bake"], "an already-baked domain must not be re-baked"
        assert len(calls["append"]) == 1
        domain, kwargs = calls["append"][0]
        assert domain == _domain_for(APP, OWNER, NS)
        # The append scan is scoped too, for the same reason the bake spec is.
        assert kwargs["owners"] == [OWNER] and kwargs["namespaces"] == [NS]
        assert kwargs["apps"] == [APP]


def test_pinned_source_still_bakes_at_task_start():
    """A concrete source keeps its start-time domain — the wildcard path must not have
    taken that over, or every existing single-namespace watch changes behaviour."""
    args = Args("/tmp/cdn")
    scoped = _source_args(args, APP, OWNER, NS)
    assert scoped.cdn_domain == _domain_for(APP, OWNER, NS)
    # …and a wildcard source deliberately has none, which is what _deliver_cdn reads
    # as "derive it per change".
    assert _source_args(args, APP, None, None).cdn_domain is None


def test_no_cdn_dir_means_no_delivery():
    args = Args(None)
    calls = _deliver(args)
    assert not calls["bake"] and not calls["append"]


if __name__ == "__main__":
    test_wildcard_change_bakes_its_own_domain()
    test_second_change_appends_instead_of_rebaking()
    test_pinned_source_still_bakes_at_task_start()
    test_no_cdn_dir_means_no_delivery()
    print("cdn wildcard delivery ok")
