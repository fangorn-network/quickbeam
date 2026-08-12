"""Audius HTTP client — stdlib only (urllib), matching the robinhood source's
no-hard-deps posture.

Audius is a decentralized protocol: there is no single API host. `https://api.audius.co`
returns the list of healthy discovery nodes; you pick one and talk to it. Every call
needs an `app_name` (their attribution convention) but NO api key — the whole catalog
is public, which is what makes this demo possible at all.
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REGISTRY = "https://api.audius.co"
FALLBACK_HOST = "https://discoveryprovider.audius.co"
APP_NAME = "fangorn-demo"

# Audius caps most list endpoints at 100 per page.
PAGE_MAX = 100

# A real User-Agent is REQUIRED, not politeness: discovery nodes sit behind
# Cloudflare, which 403s the default `Python-urllib/3.x` while letting curl through.
_HEADERS = {"Accept": "application/json",
            "User-Agent": "fangorn-demo/0.1 (+https://fangorn.network)"}


class AudiusError(RuntimeError):
    pass


class AudiusAPI:
    """Thin, polite, retrying client over one discovery node.

    Shared by the crawler across threads: `urlopen` is stateless here, and the only
    mutable state (the request counter) is behind a lock.
    """

    def __init__(self, app_name: str = APP_NAME, host: str | None = None,
                 delay: float = 0.08, retries: int = 6, timeout: int = 30):
        self.app_name = app_name
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.calls = 0
        self.throttled = 0
        self._lock = threading.Lock()
        self.host = (host or self._discover()).rstrip("/")

    def _discover(self) -> str:
        """Ask the registry for a healthy discovery node."""
        try:
            req = urllib.request.Request(REGISTRY, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                hosts = (json.load(r) or {}).get("data") or []
            for h in hosts:
                # The registry currently advertises only ITSELF — and that host is a
                # rate-limited load balancer that 429s under a real crawl. Take a
                # node only if it's genuinely a different host.
                if isinstance(h, str) and h.startswith("http") and \
                        h.rstrip("/") != REGISTRY.rstrip("/"):
                    return h
        except Exception:  # noqa: BLE001 — registry down is not fatal, we have a fallback
            pass
        return FALLBACK_HOST

    # -- core ---------------------------------------------------------------
    def get(self, path: str, **params) -> object:
        """GET {host}{path} and return the response's `data`, or None on 404.

        Retries on transport errors and 5xx/429 with exponential backoff + jitter —
        these are volunteer-run nodes, so a blip is normal and must not abort a crawl
        that is already 900 requests deep.
        """
        params.setdefault("app_name", self.app_name)
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.host}{path}?{qs}"
        last = None
        for attempt in range(self.retries):
            if self.delay:
                time.sleep(self.delay)
            try:
                with self._lock:
                    self.calls += 1
                req = urllib.request.Request(url, headers=_HEADERS)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return (json.load(r) or {}).get("data")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code not in (429, 500, 502, 503, 504):
                    raise AudiusError(f"{e.code} {url}") from e
                last = e
                if e.code == 429:
                    # Adaptive: slow the WHOLE crawl down a notch rather than
                    # retrying into the same wall. Self-tuning beats a guessed
                    # constant, since the limit depends on how loaded the node is.
                    with self._lock:
                        self.delay = min(self.delay + 0.05, 1.0)
                        self.throttled += 1
                    wait = e.headers.get("Retry-After") if e.headers else None
                    if wait and str(wait).isdigit():
                        time.sleep(min(int(wait), 30))
                        continue
            except Exception as e:  # noqa: BLE001 — timeouts, DNS, reset peers
                last = e
            time.sleep((2 ** attempt) * 0.5 + random.random() * 0.3)
        raise AudiusError(f"giving up on {url}: {last}")

    def get_list(self, path: str, **params) -> list[dict]:
        """`get` coerced to a list — several endpoints return an object or null."""
        d = self.get(path, **params)
        return d if isinstance(d, list) else []

    def paged(self, path: str, max_items: int = PAGE_MAX, **params) -> list[dict]:
        """Walk `offset`/`limit` pages until `max_items` or a short page.

        Deliberately sequential: pages are ordered and each depends on the previous
        offset, so there is nothing to parallelize inside one listing.
        """
        out: list[dict] = []
        offset = 0
        while len(out) < max_items:
            want = min(PAGE_MAX, max_items - len(out))
            page = self.get_list(path, limit=want, offset=offset, **params)
            out.extend(page)
            if len(page) < want:
                break
            offset += len(page)
        return out[:max_items]

    def parallel(self, jobs: list[tuple], fn, workers: int = 8) -> list:
        """Run `fn(job)` over `jobs` and return results in **input order**.

        Order matters beyond tidiness: the graph these results build is
        content-addressed, so a nondeterministic ordering would change the CIDs and
        break the "same cache → same commit" guarantee.
        """
        if not jobs:
            return []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(fn, jobs))
