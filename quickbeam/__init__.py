"""
quickbeam — build, publish, and serve a semantic search index over Fangorn data.

Publisher SDK surface (the "import quickbeam and publish" path):

    import quickbeam as qb
    from my_scraper import MySource                # YOUR Source — core ships none

    pub = qb.Publisher(MySource(), repo="./my-data",
                       prefix="me.mysrc", bundle_name="widgets")
    pub.run(api_url="https://example.com/api")     # onboard → ingest → publish

quickbeam is source-agnostic: it provides the framework, you bring the scraper. Define
one by implementing the `Source` contract (`read` / `build_graph` / `next_cursor` + a few
attributes — subclass `SourceBase` for the defaults), then hand it to `Publisher`. See
`docs/SCRAPER_HARNESS_AUTHORING.md` and the `quickbeam-publisher` example project.
"""
from .ingest.scrapers import Source, SourceBase
from .publish import Publisher

__version__ = "0.1.0"

__all__ = ["Publisher", "Source", "SourceBase", "__version__"]
