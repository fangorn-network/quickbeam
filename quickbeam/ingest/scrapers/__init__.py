"""
The scraper harness — pluggable ingestion adapters on one shared runtime.

A `Source` (see `source.py`) supplies read + shape + cursor; `run_source` (see
`harness.py`) provides the CLI, staged-volume emission, checkpointing, the watch
daemon, and publish-to-fangorn. See `docs/SCRAPER_HARNESS.md`.

Sources are DISCOVERED (`discover_sources`) so `cli.py`'s `data` sub-app registers
one command per source with no hand-written stanza. quickbeam core ships NO concrete
sources — every source lives in an external package that registers an entry point in
the `quickbeam.sources` group; `quickbeam data <name>` then works with the full
watch/publish/checkpoint loop for free (and the same class is usable via
`quickbeam.Publisher`). See the `quickbeam-publisher` example project.
"""
from typing import Callable

from .harness import (build_parser, compose_searchable_text, emit_volumes,
                      fangorn_commit_push, fangorn_repo_init, ingest_once,
                      run_source)
from .source import Source, SourceBase

__all__ = [
    "Source",
    "SourceBase",
    "run_source",
    "ingest_once",
    "build_parser",
    "emit_volumes",
    "compose_searchable_text",
    "fangorn_repo_init",
    "fangorn_commit_push",
    "discover_sources",
]

# quickbeam core registers NO built-in sources — the framework is source-agnostic.
# Sources are contributed entirely by external packages via `quickbeam.sources` entry
# points (see `discover_sources`). This dict exists only as the (empty) seed those
# entry points overlay; keep it empty so core never hard-codes a concrete source.
_BUILTIN: dict[str, str] = {}


def _load_spec(spec: str) -> Callable[[], type]:
    """A "module:attr" string → a zero-arg loader returning the Source class."""
    def _load() -> type:
        import importlib
        mod, _, attr = spec.partition(":")
        return getattr(importlib.import_module(mod), attr)
    return _load


def discover_sources() -> dict[str, Callable[[], type]]:
    """Map CLI verb → a lazy loader of its `Source` class. The built-in registry is
    overlaid with any entry points in the `quickbeam.sources` group, so an installed
    third-party package (or a re-installed in-tree entry point) appears automatically;
    an out-of-tree verb collides-and-overrides a built-in of the same name."""
    out: dict[str, Callable[[], type]] = {
        name: _load_spec(spec) for name, spec in _BUILTIN.items()
    }
    try:
        import importlib.metadata as _md
        eps = _md.entry_points()
        group = (eps.select(group="quickbeam.sources")
                 if hasattr(eps, "select") else eps.get("quickbeam.sources", []))
        for ep in group:
            out[ep.name] = ep.load   # ep.load() → the Source class (deferred)
    except Exception:  # noqa: BLE001 — discovery must never break the CLI
        pass
    return out
