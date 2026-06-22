"""
Thin wrappers around the upstream `cmon` CLI (CmonCrawl).

We drive CmonCrawl through its stable command-line interface instead of importing
it, because (a) it pins an old pydantic that conflicts with fastapi/mcp and (b)
the extract step runs untrusted code we want out-of-process. Install CmonCrawl in
its own venv and pass that binary via `cmon_bin` (default: `cmon` on PATH, or
`$CMON_BIN`).

CLI shapes (cmoncrawl 1.1.x):
  cmon download [--limit N] [--since ISO] [--to ISO] [--match_type T]
                [--aggregator gateway|athena] [--filter_non_200]
                OUTPUT URLS... {record,html}
  cmon extract  CONFIG OUTPUT FILES... {record,html}
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable

from quickbeam.crawl.config import CrawlQuery


def default_bin() -> str:
    return os.environ.get("CMON_BIN", "cmon")


# Stderr signatures that mean CmonCrawl couldn't reach Common Crawl's index/CDX
# server (index.commoncrawl.org) — i.e. an upstream outage, not a bad query. When
# the server list fetch fails, the gateway aggregator leaves `cc_servers` empty and
# raises this on iteration.
_INDEX_UNREACHABLE_MARKERS = (
    "cc_servers must be set before iterating",
    "Failed to get CC servers",
)


class CrawlDownloadError(RuntimeError):
    """Raised when every `cmon download` attempt failed (vs. legitimately empty).

    Distinguishes an infrastructure failure (index server down, network) — where
    *no* URL could even be queried — from a successful query that simply matched
    no captures. `index_unreachable` is True when the failures look like Common
    Crawl's index server being down, so callers can print a targeted message.
    """

    def __init__(self, failures: list[tuple[str, str]]):
        self.failures = failures
        self.index_unreachable = any(
            any(m in reason for m in _INDEX_UNREACHABLE_MARKERS)
            for _, reason in failures
        )
        urls = ", ".join(url for url, _ in failures)
        if self.index_unreachable:
            msg = ("Common Crawl index server (index.commoncrawl.org) unreachable — "
                   f"all {len(failures)} download(s) failed. This is an upstream "
                   "outage, not your query; retry when it is back "
                   "(curl -sf https://index.commoncrawl.org/collinfo.json).")
        else:
            msg = f"all {len(failures)} download(s) failed ({urls})"
        super().__init__(msg)


def download_argv(query: CrawlQuery, out_dir: str | Path, *, mode: str = "html",
                  cmon_bin: str | None = None) -> list[str]:
    argv = [cmon_bin or default_bin(), "download",
            "--limit", str(query.limit),
            "--match_type", query.match_type,
            "--aggregator", query.aggregator]
    if query.since:
        argv += ["--since", query.since]
    if query.to:
        argv += ["--to", query.to]
    if query.filter_non_200:
        argv += ["--filter_non_200"]
    argv += [str(out_dir), *query.urls, mode]
    return argv


def extract_argv(config_path: str | Path, out_dir: str | Path, files: list[str],
                 *, mode: str = "html", n_proc: int = 1,
                 cmon_bin: str | None = None) -> list[str]:
    return [cmon_bin or default_bin(), "extract",
            "--n_proc", str(n_proc),
            str(config_path), str(out_dir), *[str(f) for f in files], mode]


def download(query: CrawlQuery, out_dir: str | Path, *, mode: str = "record",
             cmon_bin: str | None = None, timeout: int = 1800) -> list[Path]:
    """
    Run `cmon download` (trusted: needs network, so NOT sandboxed) and return the
    downloaded files.

    `record` mode writes lightweight `.jsonl` record pointers (each carrying the
    capture's real URL/offset); `html` mode writes content-only `.html` files.
    The pipeline uses `record` so the extractor routes by, and sees, real URLs —
    `html` mode would route every file by a single `--url` and is unsuitable for
    multi-page crawls.

    Per-domain budget: `cmon`'s `--limit` is a *global* cap on downloaded pointers
    and it drains URLs roughly in order, so a single bot-blocked or huge domain can
    exhaust the budget and starve the others — fatal for cross-domain crawls. We
    therefore run one `cmon download` per URL (each gets the full `--limit`) and
    concatenate. One domain failing (e.g. an index error) is logged and skipped
    rather than aborting the whole crawl.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".html" if mode == "html" else ".jsonl"

    files: list[Path] = []
    failures: list[tuple[str, str]] = []
    for i, url in enumerate(query.urls):
        sub = out_dir / f"q{i}"
        sub.mkdir(parents=True, exist_ok=True)
        per_url_query = replace(query, urls=[url])
        argv = download_argv(per_url_query, sub, mode=mode, cmon_bin=cmon_bin)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"[crawl] download timed out for {url} — skipping")
            failures.append((url, "timed out"))
            continue
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "")[-500:]
            print(f"[crawl] download failed for {url} (exit {proc.returncode}); skipping:\n{detail}")
            failures.append((url, detail))
            continue
        got = sorted(p for p in sub.rglob(f"*{suffix}") if p.is_file())
        files.extend(got)

    # Every URL erroring out is an infrastructure failure (index server down,
    # network), not a legitimately empty result — surface it loudly so callers
    # don't write a green ✅ over an empty file. A partial failure (some URLs
    # succeeded) or a clean query that simply matched nothing returns normally.
    if not files and failures and len(failures) == len(query.urls):
        raise CrawlDownloadError(failures)

    return files


def extract(config_path: str | Path, out_dir: str | Path, files: list[str], *,
            mode: str = "record", n_proc: int = 1, cmon_bin: str | None = None,
            runner: Callable[[list[str]], object] | None = None) -> Path:
    """
    Run `cmon extract`. `runner` defaults to a plain subprocess but the pipeline
    passes a sandboxed runner (quickbeam.crawl.sandbox.run) so the untrusted
    extractor code is confined. Returns the output directory.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = extract_argv(config_path, out_dir, files, mode=mode, n_proc=n_proc, cmon_bin=cmon_bin)
    if runner is None:
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"cmon extract failed (exit {proc.returncode}):\n"
                f"{(proc.stderr or proc.stdout or '')[-2000:]}"
            )
    else:
        runner(argv)
    return out_dir
