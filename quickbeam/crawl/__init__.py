"""
quickbeam.crawl — on-demand Common Crawl scraping via CmonCrawl.

Given a CmonCrawl *routes config* (the exact JSON shape CmonCrawl's `cmon extract`
consumes) plus the extractor Python sources, this package crawls Common Crawl,
runs the extractors in a sandbox, and produces Fangorn-shaped ``{name, fields}``
records — the same record shape every other quickbeam pipeline emits.

The heavy lifting is delegated to the upstream ``cmon`` CLI (download + extract)
rather than importing CmonCrawl in-process: CmonCrawl pins an old pydantic that
conflicts with fastapi/mcp, and shelling out keeps the (untrusted, LLM-generated)
extractor code out of the service interpreter. Install CmonCrawl in its own venv
and point the service at that ``cmon`` binary (``--cmon-bin`` / ``$CMON_BIN``).

Public surface:
  * CrawlJob / CrawlQuery        — the parsed job description (see config.py)
  * run_crawl(job, workdir=...)  — full pipeline → list[{name, fields}]
  * CrawlDownloadError           — raised when every download attempt failed
"""

from quickbeam.crawl.cmon import CrawlDownloadError
from quickbeam.crawl.config import CrawlJob, CrawlQuery
from quickbeam.crawl.pipeline import run_crawl

__all__ = ["CrawlJob", "CrawlQuery", "run_crawl", "CrawlDownloadError"]
