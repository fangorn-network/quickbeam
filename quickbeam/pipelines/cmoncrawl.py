"""
quickbeam data crawl — one-shot, offline Common Crawl scrape + extract.

Runs the crawl pipeline locally (no Fangorn, no chain, no payment) and writes
`{name, fields}` records to JSON — the same shape `quickbeam build`/`watch`
consume. Use it to develop/test an extractor before wiring the on-chain
`crawl_job` → scraper-service path.

Example
-------
  quickbeam data crawl \
    --routes ./routes.json \
    --extractors ./my_extractors \
    --url example.com --match-type domain \
    --since 2024-01-01 --to 2024-06-01 --limit 50 \
    --out ./stage_volumes/crawl.json

`--routes` is the CmonCrawl routes array (a JSON array, or an object with a
`routes` key). `--extractors` is a directory of `<name>.py` modules (each exposing
an `extractor` variable) referenced by the routes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from quickbeam.crawl import CrawlDownloadError, run_crawl
from quickbeam.crawl.config import CrawlJob


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="One-shot Common Crawl scrape + extract → Fangorn-shaped JSON",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--routes", required=True, help="Path to routes JSON (array or {routes:[...]}).")
    p.add_argument("--extractors", required=True, help="Directory of <name>.py extractor modules.")
    p.add_argument("--url", action="append", dest="urls", required=True,
                   metavar="URL", help="URL/host to crawl, e.g. example.com. Repeatable.")
    p.add_argument("--match-type", default="domain", choices=["exact", "prefix", "host", "domain"])
    p.add_argument("--since", default=None, metavar="ISO")
    p.add_argument("--to", default=None, metavar="ISO")
    p.add_argument("--limit", type=int, default=50,
                   help="Max captures to download PER --url (each domain gets its own "
                        "budget, so a blocked/huge domain can't starve the others).")
    p.add_argument("--aggregator", default="gateway", choices=["gateway", "athena", "free"],
                   help="gateway: CDX query API (index.commoncrawl.org — often down); "
                        "athena: AWS Athena (needs credentials, billed); "
                        "free: read the columnar index directly off data.commoncrawl.org "
                        "(no CDX server, no AWS) — use this when gateway is down.")
    p.add_argument("--cc-crawl", action="append", dest="cc_crawls", metavar="CC-MAIN-YYYY-WW",
                   help="Crawl id(s) for --aggregator free (repeatable). Default: latest crawl.")
    p.add_argument("--output-schema", default="local.crawl.v1",
                   help="Name used only to seed record-name prefixes locally.")
    p.add_argument("--cmon-bin", default=None, help="Path to the cmon binary (default: $CMON_BIN or 'cmon').")
    p.add_argument("--n-proc", type=int, default=4,
                   help="Parallel worker processes for `cmon extract`. In record mode "
                        "extract is network-bound (it fetches each capture's WARC content "
                        "as it runs), so >1 is needed to keep a wide multi-domain crawl "
                        "inside --extract-timeout.")
    p.add_argument("--extract-timeout", type=int, default=1800,
                   help="Wall-clock seconds for the extract step before it is killed. "
                        "Raise for large crawls (many URLs × high --limit).")
    p.add_argument("--no-sandbox", action="store_true", help="Run extract without the network namespace isolation.")
    p.add_argument("--out", required=True, help="Output JSON path.")
    return p.parse_args(argv)


def _load_routes(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    routes = data.get("routes") if isinstance(data, dict) else data
    if not isinstance(routes, list) or not routes:
        raise SystemExit(f"--routes must be a non-empty array (or object with 'routes'): {path}")
    return routes


def _load_extractor_sources(routes: list[dict], extractors_dir: str) -> dict[str, str]:
    ex_dir = Path(extractors_dir)
    names: set[str] = set()
    for route in routes:
        for ex in route.get("extractors", []) or []:
            if isinstance(ex, dict) and ex.get("name"):
                names.add(str(ex["name"]))
    sources: dict[str, str] = {}
    for name in names:
        f = ex_dir / f"{name}.py"
        if not f.exists():
            raise SystemExit(f"extractor module not found: {f}")
        sources[name] = f.read_text(encoding="utf-8")
    return sources


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    routes = _load_routes(args.routes)
    sources = _load_extractor_sources(routes, args.extractors)

    job = CrawlJob.from_fields({
        "routes": routes,
        "extractors": sources,
        "query": {
            "urls": args.urls,
            "matchType": args.match_type,
            "since": args.since,
            "to": args.to,
            "limit": args.limit,
            "aggregator": args.aggregator,
        },
        "outputSchema": args.output_schema,
    })

    # Pick the download backend. `free` bypasses the CDX query server entirely by
    # reading the columnar index off data.commoncrawl.org (works during gateway
    # outages, no AWS); gateway/athena use cmon's own download.
    download_fn = None
    if args.aggregator == "free":
        import functools
        from quickbeam.crawl import freeindex
        download_fn = functools.partial(freeindex.download, crawls=args.cc_crawls or None)

    from quickbeam.crawl.pipeline import SandboxLimits
    try:
        records = run_crawl(
            job,
            cmon_bin=args.cmon_bin,
            # Record-mode extract is network-bound, so the wall clock (not CPU) is
            # the real limit; align cpu_s with it to avoid a spurious RLIMIT_CPU
            # (SIGXCPU) kill on a long, parallel crawl.
            limits=SandboxLimits(
                no_network=not args.no_sandbox,
                timeout=args.extract_timeout,
                cpu_s=args.extract_timeout,
            ),
            n_proc=args.n_proc,
            download_fn=download_fn,
        )
    except CrawlDownloadError as e:
        # Every download failed — an infrastructure problem, not an empty result.
        # Exit non-zero and write nothing, rather than a green ✅ over an empty file.
        raise SystemExit(f"❌ crawl aborted: {e}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ wrote {len(records)} record(s) → {out}", file=sys.stderr)
    if records:
        print(f"   sample fields: {list(records[0]['fields'].keys())}", file=sys.stderr)


if __name__ == "__main__":
    main()
