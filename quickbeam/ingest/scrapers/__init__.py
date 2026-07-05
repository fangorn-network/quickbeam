"""
The scraper harness — pluggable ingestion adapters on one shared runtime.

A `Source` (see `source.py`) supplies read + shape + cursor; `run_source` (see
`harness.py`) provides the CLI, staged-volume emission, checkpointing, the watch
daemon, and publish-to-fangorn. See `docs/SCRAPER_HARNESS.md`.

Sources are DISCOVERED (`discover_sources`) so `cli.py`'s `data` sub-app registers
one command per source with no hand-written stanza. In-tree sources live in the
built-in registry below; a third-party pip package registers by adding an entry point
to the `quickbeam.sources` group — `quickbeam data <name>` then works with the full
watch/publish/checkpoint loop for free.
"""
from typing import Callable

from .harness import compose_searchable_text, emit_volumes, run_source
from .source import Source, SourceBase

__all__ = [
    "Source",
    "SourceBase",
    "run_source",
    "emit_volumes",
    "compose_searchable_text",
    "discover_sources",
]

# ── Built-in registry — CLI verb → "module:ClassName" for the in-tree sources.
# The verb is the `data <verb>` command name; it need not equal Source.name (e.g.
# `eventspg` runs EventsSource). Kept as strings so the (heavier) source modules are
# imported only when a command actually runs, keeping CLI startup light.
_BUILTIN: dict[str, str] = {
    "robinhood": "quickbeam.ingest.scrapers.robinhood:RobinhoodSource",
    "osm":       "quickbeam.ingest.scrapers.osm:OsmSource",
    "eventspg":  "quickbeam.ingest.scrapers.events:EventsSource",
    "placespg":  "quickbeam.ingest.scrapers.places:PlacesSource",
}


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
