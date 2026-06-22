"""
Crawl job description — the parsed form of a `crawl_job` manifest entry.

A `crawl_job` manifest entry published on Fangorn carries (under `fields`):

    {
      "routes": [                          # exactly CmonCrawl's extract `routes`
        {"regexes": [".*"],
         "extractors": [{"name": "my_extractor", "since": "...", "to": "..."}]}
      ],
      "extractors": {                      # name -> python source (inline)
        "my_extractor": "from cmoncrawl...\nextractor = MyExtractor()\n"
      },
      "extractorRefs": {                   # OR name -> IPFS dataCid (resolved by caller)
        "my_extractor": "bafy..."
      },
      "query": {                           # what slice of Common Crawl to pull
        "urls": ["example.com"],
        "matchType": "domain",            # exact | prefix | host | domain
        "since": "2024-01-01",
        "to":    "2024-06-01",
        "limit": 100,
        "aggregator": "gateway",          # gateway (free) | athena (AWS, $)
        "filterNon200": true
      },
      "outputSchema": "fangorn.webpage.v1",
      "outputSchemaDef": { ... },          # optional resolver SchemaDefinition
      "paymentReceipt": "local:0x..."      # x402 settlement reference (audit)
    }

`extractors` (inline source) takes precedence over `extractorRefs`. The route
`extractors[].name` must match both a key here and the module file the publisher
ships (`<name>.py`, exposing an `extractor` variable — CmonCrawl's contract).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


_MATCH_TYPES = {"exact", "prefix", "host", "domain"}
_AGGREGATORS = {"gateway", "athena", "free"}


def _parse_extractors(fields: dict) -> tuple[dict[str, str], dict[str, str]]:
    """
    Resolve the `extractors` field into (name->source, name->cid) maps.

    Canonical (on-chain, matches schemas/crawl_job.schema.json) form is a typed
    array of `extractorModule` objects: [{name, source?, sourceCid?, language?}].
    A plain {name: source} map is also accepted as an offline convenience, as is a
    separate legacy `extractorRefs` {name: cid} map.
    """
    sources: dict[str, str] = {}
    refs: dict[str, str] = {}
    raw = fields.get("extractors")
    if isinstance(raw, dict):
        sources = {str(k): v for k, v in raw.items() if isinstance(v, str)}
    elif isinstance(raw, list):
        for mod in raw:
            if not isinstance(mod, dict):
                continue
            name = mod.get("name")
            if not name:
                continue
            name = str(name)
            if isinstance(mod.get("source"), str) and mod["source"]:
                sources[name] = mod["source"]
            if isinstance(mod.get("sourceCid"), str) and mod["sourceCid"]:
                refs[name] = mod["sourceCid"]
    for k, v in (fields.get("extractorRefs") or fields.get("extractor_refs") or {}).items():
        if isinstance(v, str) and v:
            refs.setdefault(str(k), v)
    return sources, refs


@dataclass
class CrawlQuery:
    urls: list[str]
    match_type: str = "domain"
    since: str | None = None
    to: str | None = None
    limit: int = 100
    aggregator: str = "gateway"
    filter_non_200: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "CrawlQuery":
        urls = d.get("urls") or ([d["url"]] if d.get("url") else [])
        if not urls:
            raise ValueError("crawl query requires 'urls' (or 'url')")
        match_type = str(d.get("matchType", d.get("match_type", "domain"))).lower()
        if match_type not in _MATCH_TYPES:
            raise ValueError(f"matchType must be one of {_MATCH_TYPES}, got {match_type!r}")
        aggregator = str(d.get("aggregator", "gateway")).lower()
        if aggregator not in _AGGREGATORS:
            raise ValueError(f"aggregator must be one of {_AGGREGATORS}, got {aggregator!r}")
        return cls(
            urls=[str(u) for u in urls],
            match_type=match_type,
            since=d.get("since"),
            to=d.get("to"),
            limit=int(d.get("limit", 100)),
            aggregator=aggregator,
            filter_non_200=bool(d.get("filterNon200", d.get("filter_non_200", True))),
        )


@dataclass
class CrawlJob:
    routes: list[dict]
    query: CrawlQuery
    output_schema: str
    # name -> python source string (inline). Wins over extractor_refs.
    extractor_sources: dict[str, str] = field(default_factory=dict)
    # name -> IPFS dataCid; resolved to source by the caller before run.
    extractor_refs: dict[str, str] = field(default_factory=dict)
    output_schema_def: dict | None = None
    payment_receipt: str | None = None

    @classmethod
    def from_fields(cls, fields: dict) -> "CrawlJob":
        routes = fields.get("routes")
        if not isinstance(routes, list) or not routes:
            raise ValueError("crawl_job requires a non-empty 'routes' array")
        query = CrawlQuery.from_dict(fields.get("query") or {})
        output_schema = fields.get("outputSchema") or fields.get("output_schema")
        if not output_schema:
            raise ValueError("crawl_job requires 'outputSchema'")
        sources, refs = _parse_extractors(fields)
        return cls(
            routes=routes,
            query=query,
            output_schema=str(output_schema),
            extractor_sources=sources,
            extractor_refs=refs,
            output_schema_def=fields.get("outputSchemaDef") or fields.get("output_schema_def"),
            payment_receipt=fields.get("paymentReceipt") or fields.get("payment_receipt"),
        )

    def payment_object(self) -> dict | None:
        """
        Decode the x402 payment authorization embedded in the manifest.

        `paymentReceipt` may be the payment object itself (a dict with
        `x402Version`/`payload`) or its base64 `X-PAYMENT` header encoding. The
        signed ERC-3009 transferWithAuthorization it carries is a single-use
        bearer authorization (nonce-protected, fixed amount, fixed recipient), so
        it is safe to publish in a manifest. Returns None if absent/unparseable.
        """
        receipt = self.payment_receipt
        if isinstance(receipt, dict):
            return receipt if receipt.get("payload") else None
        if isinstance(receipt, str) and receipt.strip():
            from quickbeam import x402 as _x402
            try:
                return _x402.decode_payment_header(receipt.strip())
            except Exception:  # noqa: BLE001 — malformed receipt → treat as unpaid
                return None
        return None

    def extractor_names(self) -> set[str]:
        """Every extractor name referenced by the routes."""
        names: set[str] = set()
        for route in self.routes:
            for ex in route.get("extractors", []) or []:
                name = ex.get("name") if isinstance(ex, dict) else None
                if name:
                    names.add(str(name))
        return names

    def payment_key(self) -> str:
        """
        Stable hash over the billable content (routes + query + output schema),
        independent of inline source/comments. The x402 paid-ledger is keyed by
        this so a quote paid before publishing matches the job manifest that
        later arrives on-chain. See scraper_service payment reconciliation.
        """
        canonical = json.dumps(
            {
                "routes": self.routes,
                "query": {
                    "urls": sorted(self.query.urls),
                    "matchType": self.query.match_type,
                    "since": self.query.since,
                    "to": self.query.to,
                    "limit": self.query.limit,
                },
                "outputSchema": self.output_schema,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def payment_key_for_fields(fields: dict) -> str:
    """Compute the same payment key directly from raw manifest/quote fields."""
    return CrawlJob.from_fields(fields).payment_key()
