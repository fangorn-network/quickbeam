"""
Back-compat shim — the places pipeline moved to `quickbeam.ingest.scrapers.places`.

The shape/merge logic + the Postgres/JSONL read now live in
`quickbeam.ingest.scrapers.places` (as `PlacesSource`) on the shared ingest harness;
running it now also gets the shared `--watch`/`--publish` flags for free. `cli.py`
discovers it via the `quickbeam.sources` registry (verb `placespg`).

New code should import from `quickbeam.ingest.scrapers.places` directly.
"""
from quickbeam.ingest.scrapers.places import (  # noqa: F401
    PlacesSource,
    iter_jsonl_rows,
    run,
    shape_business,
    shape_category,
    shape_review,
)


if __name__ == "__main__":
    run()
