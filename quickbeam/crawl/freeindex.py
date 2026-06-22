"""
Free Common Crawl index reader — a drop-in replacement for `cmon download` that
does NOT depend on the CDX query API (index.commoncrawl.org).

Why this exists
---------------
`index.commoncrawl.org` is a single EC2 host serving the CDX query API. It goes
down / refuses connections for extended periods; when it does, CmonCrawl's gateway
aggregator fails with ``cc_servers must be set before iterating`` and no crawl can
run (see quickbeam.crawl.cmon.CrawlDownloadError).

The index *data* itself, however, lives on ``data.commoncrawl.org`` — the same
CDN-backed host cmon's record-mode `extract` already fetches WARCs from — and is
free to read (no AWS, no Athena). Each crawl publishes a columnar index:

  cc-index/collections/<CRAWL>/indexes/cluster.idx   # sorted secondary index
  cc-index/collections/<CRAWL>/indexes/cdx-NNNNN.gz  # gzip-blocked CDXJ shards

`cluster.idx` is sorted by SURT key and lists, per compressed cdx block, that
block's first key plus its (cdx file, byte offset, length). So to answer a query
we binary-search cluster.idx with HTTP range requests (no full download), then
range-fetch + gunzip only the matching block(s) and filter their CDXJ lines.

This module exposes :func:`download` with the same signature as
``cmon.download`` so the pipeline's ``download_fn`` seam can swap it in. It writes
cmon-compatible record ``.jsonl`` pointers (``{"domain_record": {...}}``); cmon's
``extract record`` then fetches the WARCs from ``data.commoncrawl.org`` and runs
the extractors unchanged. The dead CDX host is never touched.
"""

from __future__ import annotations

import gzip
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from quickbeam.crawl.cmon import CrawlDownloadError
from quickbeam.crawl.config import CrawlQuery

DATA_HOST = "https://data.commoncrawl.org"
_COLLECTIONS = f"{DATA_HOST}/cc-index/collections"
_USER_AGENT = "quickbeam-crawl (+https://github.com/; free-index reader)"


# ── HTTP helpers (stdlib only; range requests against the static data host) ──────
def _get(url: str, *, range_: tuple[int, int] | None = None, timeout: int = 60) -> bytes:
    headers = {"User-Agent": _USER_AGENT}
    if range_ is not None:
        headers["Range"] = f"bytes={range_[0]}-{range_[1]}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _content_length(url: str, *, timeout: int = 60) -> int:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(r.headers["Content-Length"])


# ── SURT keys ────────────────────────────────────────────────────────────────
def _surt_host(host: str) -> str:
    """Reverse-domain SURT of a host, matching CC canonicalization (drops www.).

    ``www.allrecipes.com`` -> ``com,allrecipes``  (no trailing ')').
    """
    host = host.lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    # Strip an explicit port if present.
    host = host.split(":", 1)[0]
    return ",".join(reversed(host.split(".")))


def _query_keys(url: str, match_type: str) -> tuple[str, str]:
    """Return ``(search_key, prefix)`` SURT bounds for a query.

    ``search_key`` is where we binary-search cluster.idx; ``prefix`` is the string
    a capture's full SURT key must start with to count as a match.
    """
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    host_surt = _surt_host(parts.netloc or parts.path)

    if match_type == "domain":
        # Registered domain incl. subdomains: "com,allrecipes)" and "com,allrecipes,*".
        return host_surt + ")", host_surt
    if match_type == "host":
        return host_surt + ")", host_surt + ")"
    if match_type == "exact":
        key = host_surt + ")" + (parts.path or "/")
        return key, key
    # "prefix" (default): URL-prefix within the host.
    path = parts.path or "/"
    key = host_surt + ")" + path
    return key, key


def _line_key(line: bytes) -> bytes:
    # cluster.idx / cdxj line begins with: "<surt> <timestamp>\t..." — the SURT key
    # is everything before the first space.
    return line.split(b" ", 1)[0]


# ── candidate-URL pre-filter ─────────────────────────────────────────────────────
# The extract step is network-bound (it fetches each capture's WARC), so its
# wall-clock budget is the real bottleneck. Spending it on dupes and non-content
# pages (print/feed/tag/comment-page variants, tracking-param duplicates) starves
# the real article pages. We drop those candidates *before* they reach extract, and
# dedupe by a canonical URL so ?utm=/print/comment-page copies of one page collapse
# to a single fetch. This is what lets a fixed extract budget land on real content.
_SKIP_PATH_MARKERS = (
    "/print/", "/feed/", "/tag/", "/tags/", "/category/", "/categories/",
    "/author/", "/comment-page-", "/wp-json/", "/wp-content/", "/cdn-cgi/",
    "/search", "/cart", "/checkout", "/account", "/login",
)
_SKIP_PAGE_RE = re.compile(r"/page/\d+/?$")


def _canonical_url(url: str | None) -> str | None:
    """Canonicalize a capture URL for dedupe, or return None to skip it entirely.

    Strips the query string and fragment (so ?utm_source=/?referer= variants of one
    page collapse), drops a trailing /print/ or /comment-page-N/ suffix, and rejects
    non-content paths. Same-page variants therefore map to one canonical URL; the
    caller dedupes on it so each underlying page is fetched at most once."""
    if not url:
        return None
    u = url.split("#", 1)[0].split("?", 1)[0]
    low = u.lower()
    if _SKIP_PAGE_RE.search(low):
        return None
    if any(m in low for m in _SKIP_PATH_MARKERS):
        return None
    # /recipe-name/print/ and /recipe-name/comment-page-2/ are variants of one page.
    u = re.sub(r"/(print|comment-page-\d+)/?$", "/", u)
    return u.rstrip("/") or u


# ── cluster.idx: find candidate cdx blocks for a key ─────────────────────────────
def _read_line_at(url: str, off: int, size: int, timeout: int, window: int = 16384) -> tuple[bytes, int] | None:
    """Return (line, line_start) for the first complete line at/after byte ``off``."""
    while off < size:
        end = min(off + window, size) - 1
        data = _get(url, range_=(off, end), timeout=timeout)
        if not data:
            return None
        start = off
        if off > 0:
            nl = data.find(b"\n")
            if nl == -1:
                off = end + 1
                continue
            data = data[nl + 1:]
            start = off + nl + 1
        nl = data.find(b"\n")
        if nl == -1:
            if end + 1 >= size:  # last line, no trailing newline
                return data, start
            window *= 4
            continue
        return data[:nl], start
    return None


def _cluster_blocks(crawl: str, search_key: str, prefix: str, *, timeout: int) -> list[tuple[str, int, int]]:
    """Binary-search cluster.idx for ``search_key``; return (cdx_file, offset, length)
    for every block that may contain captures whose SURT starts with ``prefix``."""
    url = f"{_COLLECTIONS}/{crawl}/indexes/cluster.idx"
    size = _content_length(url, timeout=timeout)
    key_b = search_key.encode()

    # Find the start offset of the rightmost block whose first key <= search_key:
    # that block may contain the first matching capture even though its first key
    # sorts before the query.
    lo, hi = 0, size
    start_off = 0
    for _ in range(40):
        if lo >= hi:
            break
        mid = (lo + hi) // 2
        got = _read_line_at(url, mid, size, timeout)
        if got is None:
            break
        line, line_start = got
        if _line_key(line) <= key_b:
            start_off = line_start
            lo = line_start + len(line) + 1
        else:
            hi = mid

    # Stream forward from start_off, collecting blocks until a block's first key
    # can no longer overlap the prefix.
    prefix_b = prefix.encode()
    blocks: list[tuple[str, int, int]] = []
    off = start_off
    while off < size:
        chunk = _get(url, range_=(off, min(off + 262144, size) - 1), timeout=timeout)
        if not chunk:
            break
        # Drop a trailing partial line so each parsed line is complete.
        last_nl = chunk.rfind(b"\n")
        consumed = len(chunk) if last_nl == -1 else last_nl + 1
        for raw in chunk[:consumed].split(b"\n"):
            if not raw:
                continue
            cols = raw.split(b"\t")
            if len(cols) < 4:
                continue
            first_key = _line_key(raw)
            cdx_file, b_off, b_len = cols[1], cols[2], cols[3]
            # A block can hold prefix matches if its first key is <= the prefix's
            # exclusive upper bound. Once first_key sorts past the prefix, stop.
            if first_key > prefix_b and not first_key.startswith(prefix_b):
                return blocks
            blocks.append((cdx_file.decode(), int(b_off), int(b_len)))
        off += consumed
        if consumed == 0:
            break
    return blocks


# ── cdx blocks: decode CDXJ lines into record pointers ───────────────────────────
def _iso_ts(ts: str) -> str | None:
    try:
        return datetime.strptime(ts, "%Y%m%d%H%M%S").isoformat()
    except ValueError:
        return None


def _block_records(crawl: str, block: tuple[str, int, int], prefix: str, *, timeout: int):
    """Yield DomainRecord dicts for HTML 200 captures in one cdx block matching prefix."""
    cdx_file, off, length = block
    url = f"{_COLLECTIONS}/{crawl}/indexes/{cdx_file}"
    raw = _get(url, range_=(off, off + length - 1), timeout=timeout)
    try:
        text = gzip.decompress(raw)
    except (OSError, EOFError):
        return
    prefix_b = prefix.encode()
    for line in text.split(b"\n"):
        if not line or not _line_key(line).startswith(prefix_b):
            continue
        try:
            _surt, _ts, blob = line.split(b" ", 2)
            meta = json.loads(blob)
        except (ValueError, json.JSONDecodeError):
            continue
        # Facts-only crawl: only successful HTML captures are worth extracting.
        if meta.get("status") != "200":
            continue
        mime = (meta.get("mime") or meta.get("mime-detected") or "").lower()
        if mime and "html" not in mime:
            continue
        if not meta.get("filename"):
            continue
        yield {
            "filename": meta["filename"],
            "url": meta.get("url"),
            "offset": int(meta["offset"]),
            "length": int(meta["length"]),
            "digest": meta.get("digest"),
            "timestamp": _iso_ts(_ts.decode()),
        }


def query_url(url: str, *, match_type: str, limit: int, crawls: list[str], timeout: int):
    """Yield up to ``limit`` record pointers for ``url`` across ``crawls``."""
    search_key, prefix = _query_keys(url, match_type)
    seen: set[str] = set()
    seen_urls: set[str] = set()
    n = 0
    for crawl in crawls:
        blocks = _cluster_blocks(crawl, search_key, prefix, timeout=timeout)
        for block in blocks:
            for rec in _block_records(crawl, block, prefix, timeout=timeout):
                # Drop non-content / duplicate URL variants before they cost an
                # extract-time WARC fetch (see _canonical_url).
                canon = _canonical_url(rec.get("url"))
                if canon is None or canon in seen_urls:
                    continue
                digest = rec.get("digest")
                if digest and digest in seen:  # dedupe identical captures across crawls
                    continue
                seen_urls.add(canon)
                if digest:
                    seen.add(digest)
                yield rec
                n += 1
                if n >= limit:
                    return


# ── crawl discovery ──────────────────────────────────────────────────────────
def list_recent_crawls(n: int = 1, *, timeout: int = 60) -> list[str]:
    """Newest-first list of recent crawl ids, read from the data host (free)."""
    import re

    html = _get(f"{_COLLECTIONS}/index.html", timeout=timeout).decode("utf-8", "replace")
    ids = sorted(set(re.findall(r"CC-MAIN-\d{4}-\d{2}", html)), reverse=True)
    if not ids:
        raise CrawlDownloadError([(f"{_COLLECTIONS}/index.html", "no crawl ids found in listing")])
    return ids[:n]


# ── drop-in replacement for cmon.download ────────────────────────────────────────
def download(query: CrawlQuery, out_dir: str | Path, *, mode: str = "record",
             cmon_bin: str | None = None, timeout: int = 1800,
             crawls: list[str] | None = None) -> list[Path]:
    """Resolve ``query`` against the free columnar index and write cmon record files.

    Signature mirrors :func:`quickbeam.crawl.cmon.download` so it can be injected as
    the pipeline's ``download_fn``. Only ``record`` mode is supported (the only mode
    the pipeline uses); ``cmon_bin`` is accepted for signature-compatibility and
    ignored. Raises :class:`CrawlDownloadError` if every URL failed.
    """
    if mode != "record":
        raise ValueError(f"freeindex.download only supports record mode, got {mode!r}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    crawls = crawls or list_recent_crawls(1, timeout=60)
    print(f"[crawl] free index: querying {len(crawls)} crawl(s) on data.commoncrawl.org "
          f"({', '.join(crawls)})")

    files: list[Path] = []
    failures: list[tuple[str, str]] = []
    for i, url in enumerate(query.urls):
        sub = out_dir / f"q{i}"
        sub.mkdir(parents=True, exist_ok=True)
        dest = sub / "records.jsonl"
        try:
            count = 0
            with dest.open("w", encoding="utf-8") as fh:
                for rec in query_url(url, match_type=query.match_type, limit=query.limit,
                                     crawls=crawls, timeout=min(timeout, 120)):
                    fh.write(json.dumps({"domain_record": rec, "additional_info": {}}) + "\n")
                    count += 1
            if count:
                print(f"[crawl] free index: {count} capture(s) for {url}")
                files.append(dest)
            else:
                print(f"[crawl] free index: no captures for {url} (matched nothing)")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"[crawl] free index failed for {url}: {exc}")
            failures.append((url, str(exc)))

    if not files and failures and len(failures) == len(query.urls):
        raise CrawlDownloadError(failures)
    return files
