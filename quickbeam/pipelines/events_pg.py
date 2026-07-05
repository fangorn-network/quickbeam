"""
Back-compat shim — the events pipeline moved to `quickbeam.ingest.scrapers.events`.

The normalize/shape/merge logic + the Postgres/JSONL read now live in
`quickbeam.ingest.scrapers.events` (as `EventsSource`) on the shared ingest harness;
running it now also gets the shared `--watch`/`--publish` flags for free. `cli.py`
imports `run` from here unchanged.

New code should import from `quickbeam.ingest.scrapers.events` directly.
"""
from quickbeam.ingest.scrapers.events import (  # noqa: F401
    EventsSource,
    load_businesses,
    match_business,
    normalize_eventbrite,
    normalize_tribe,
    run,
    shape_event,
    shape_organizer,
)


if __name__ == "__main__":
    run()
