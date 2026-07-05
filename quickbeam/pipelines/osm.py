"""
Back-compat shim — the OSM pipeline moved to `quickbeam.ingest.scrapers.osm`.

The shaper + Overpass/Wikidata read now live in `quickbeam.ingest.scrapers.osm`
(as `OsmSource`) on the shared ingest harness; running it now also gets the shared
`--watch`/`--publish` flags for free. `cli.py` imports `main` from here unchanged.

New code should import from `quickbeam.ingest.scrapers.osm` directly.
"""
from quickbeam.ingest.scrapers.osm import (  # noqa: F401
    LAYERS,
    OsmSource,
    build_layer_query,
    lookup_bbox,
    overpass_request,
    resolve_wikidata_images,
    run,
    shape_node,
)

# The old CLI entrypoint was `main`; the harness names it `run`.
main = run


if __name__ == "__main__":
    run()
