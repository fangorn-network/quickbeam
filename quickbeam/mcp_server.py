"""
mcp_server.py — Model Context Protocol layer over the quickbeam search API.

A REMOTE MCP server (streamable-http) that exposes the catalog's semantic search
to agents — Claude, autonomous agents, mobile assistants — as well-typed tools.

Design
------
This is a THIN HTTP client of the quickbeam FastAPI (default http://localhost:8080).
It holds no embedding model and no Qdrant client; every tool delegates to the API
and reshapes the response for an agent. Keeping it stateless means the same MCP
server works over a music corpus today and an OSM corpus tomorrow by changing
config (corpus label + domain string) — never tool logic. The shape of each
result is driven by the API's role map (GET /schema), so nothing here is
music-specific.

Every result carries on-chain provenance (source CID, publish time, version,
publisher) as a first-class field. On music that is just where-it-came-from; it
is the foundation of the multi-source story, so it is never omitted.

Payments are PHASED and isolated:
  • Phase 1 (default): tools are free. The /search calls over localhost are
    ungated. This module stays clean — no payment code runs.
  • Phase 2 (--x402-pay-to set): each gated tool charges the calling agent per
    call. All of that logic lives in quickbeam/mcp_payments.py; here it is a
    single guard line per tool plus an optional `payment` argument.

Run:
    # local stdio (MCP Inspector / Claude Desktop):
    quickbeam mcp --transport stdio --api-url http://localhost:8080

    # remote streamable-http:
    quickbeam mcp --transport http --host 0.0.0.0 --port 8765 \
        --api-url http://localhost:8080

    # Phase 2 (charge external agents per call):
    quickbeam mcp --transport http --x402-pay-to 0xRECV --x402-price 0.001
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

import httpx
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# CONFIG — resolved from env at import; overridable in main().
# The domain/corpus strings are the OSM-switch seam: change these (not code).
# ---------------------------------------------------------------------------
API_URL = os.environ.get("QUICKBEAM_API_URL", "http://localhost:8080")
CORPUS  = os.environ.get("QUICKBEAM_CORPUS", "fangorn-music")
DOMAIN  = os.environ.get(
    "QUICKBEAM_DOMAIN",
    "music tracks — searchable by vibe, mood, genre, instrumentation and lyrical "
    "theme, not just exact keywords",
)
API_TIMEOUT = float(os.environ.get("QUICKBEAM_API_TIMEOUT", "60"))

# Phase 2 gate; stays None (and dormant) unless main() enables payments.
_GATE = None

mcp = FastMCP("quickbeam")


# ---------------------------------------------------------------------------
# HTTP PLUMBING (delegates everything to the FastAPI)
# ---------------------------------------------------------------------------
def _client() -> httpx.AsyncClient:
    """Factory for the API client. Tests monkeypatch this to point at an
    in-process ASGI app."""
    return httpx.AsyncClient(base_url=API_URL, timeout=API_TIMEOUT)


async def _api_get(path: str, params: dict | None = None) -> dict:
    async with _client() as client:
        resp = await client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


# Role map is identical for the life of a collection — fetch once.
_roles_cache: dict | None = None
_roles_lock = asyncio.Lock()


async def _get_roles() -> dict:
    global _roles_cache
    if _roles_cache is None:
        async with _roles_lock:
            if _roles_cache is None:
                schema = await _api_get("/schema")
                _roles_cache = schema.get("roles", {}) or {}
    return _roles_cache


# ---------------------------------------------------------------------------
# RESULT SHAPING (schema-generic, via the role map)
# ---------------------------------------------------------------------------
def _iso8601(ts) -> str | None:
    """Best-effort ISO8601 from a unix-seconds blockTimestamp."""
    if ts in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _shape_hit(hit: dict, roles: dict) -> dict:
    """Project a raw API hit into an agent-friendly, schema-generic record.
    Drops the embedding vector (token bloat, useless to the caller)."""
    fields = hit.get("fields", {}) or {}

    title_field    = roles.get("title")
    subtitle_field = roles.get("subtitle")
    title    = fields.get(title_field)    if title_field    else None
    subtitle = fields.get(subtitle_field) if subtitle_field else None

    tags: list[str] = []
    for f in roles.get("tags", []) or []:
        v = fields.get(f)
        if isinstance(v, list):
            tags.extend(str(x) for x in v if x)
        elif v:
            tags.append(str(v))

    meta = hit.get("meta", {}) or {}
    provenance = {
        "source_cid": meta.get("manifestCid"),
        "published":  _iso8601(meta.get("blockTimestamp")),
        "version":    meta.get("version"),
        "publisher":  meta.get("owner"),
    }

    return {
        "id":         hit.get("id"),
        "title":      title,
        "subtitle":   subtitle,
        "tags":       tags,
        "score":      hit.get("score"),
        "provenance": provenance,
    }


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------
@mcp.tool
async def semantic_search(query: str, limit: int = 10, payment: str | None = None) -> dict:
    """Search the catalog by MEANING, not keywords.

    The corpus is %(domain)s. Use this whenever the user describes what they want
    in natural language — a mood, vibe, activity, scene, era, or feeling
    ("rainy-day melancholy piano", "high-energy tracks for a workout",
    "songs that feel like a road trip at night") — rather than an exact title or
    name. The query is embedded and matched against the catalog's vector space,
    so loose, descriptive, and metaphorical phrasing all work well; you do not
    need the user to name anything that exists in the catalog.

    Args:
        query: A natural-language description of what to find. Descriptive and
            vibe-based phrasing is ideal; exact keywords are not required.
        limit: Maximum number of results to return (default 10).

    Returns:
        { "results": [ {
              "id", "title", "subtitle", "tags": [...], "score": float,
              "provenance": { "source_cid", "published", "version", "publisher" }
          } ... ],
          "corpus": "%(corpus)s" }

    Every result includes `provenance` — where the record came from on-chain
    (its source CID, publish time, version, and publisher address). Surface or
    cite it when the user cares about freshness or origin. Results are ordered by
    relevance `score` (higher is closer).
    """ % {"domain": DOMAIN, "corpus": CORPUS}
    settlement = None
    if _GATE is not None:
        charge = _GATE.charge("semantic_search", payment)
        if not charge.ok:
            return charge.challenge        # x402 requirements back to the agent
        settlement = charge.settlement

    roles = await _get_roles()
    try:
        data = await _api_get("/search", {"q": query, "n_results": limit})
    except httpx.HTTPError as exc:
        return {"error": "search_failed", "detail": str(exc)}

    out = {
        "results": [_shape_hit(h, roles) for h in data.get("results", [])],
        "corpus":  CORPUS,
    }
    if settlement is not None:
        out["payment"] = settlement
    return out


@mcp.tool
async def corpus_info() -> dict:
    """Describe the catalog this server searches: its domain, the field roles
    (which fields are title / subtitle / tags), and how many records it holds.
    Call this first if you are unsure whether this corpus is relevant to the
    user's request. Free — no payment required."""
    roles = await _get_roles()
    try:
        health = await _api_get("/health")
        count  = health.get("count")
    except httpx.HTTPError:
        count = None
    return {
        "corpus":      CORPUS,
        "domain":      DOMAIN,
        "record_count": count,
        "roles": {
            "title":    roles.get("title"),
            "subtitle": roles.get("subtitle"),
            "tags":     roles.get("tags", []),
        },
        "paid": _GATE is not None,
    }


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
def main() -> None:
    global API_URL, CORPUS, DOMAIN, _GATE

    parser = argparse.ArgumentParser(description="quickbeam MCP server")
    parser.add_argument("--api-url", default=API_URL,
                        help="Base URL of the quickbeam HTTP API.")
    parser.add_argument("--corpus", default=CORPUS,
                        help="Corpus label returned with results.")
    parser.add_argument("--domain", default=DOMAIN,
                        help="One-line description of the corpus domain (drives tool docs).")
    parser.add_argument("--transport", default="http",
                        choices=["http", "streamable-http", "stdio", "sse"],
                        help="MCP transport (default: http = streamable-http).")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (http/sse).")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (http/sse).")
    # ── Phase 2: charge external agents per tool call ────────────────────────
    x = parser.add_argument_group("x402 per-tool payment (Phase 2)")
    x.add_argument("--x402-pay-to", default=None, metavar="0x...",
                   help="Recipient address. Enables per-tool payment when set.")
    x.add_argument("--x402-price", default="0.001", metavar="USDC",
                   help="Price per tool call in whole token units.")
    x.add_argument("--x402-network", default="base-sepolia")
    x.add_argument("--x402-asset", default=None, metavar="0x...")
    x.add_argument("--x402-decimals", type=int, default=6)
    x.add_argument("--x402-facilitator", default=None, metavar="URL")
    args = parser.parse_args()

    API_URL = args.api_url
    CORPUS  = args.corpus
    DOMAIN  = args.domain

    if args.x402_pay_to:
        try:
            from quickbeam.mcp_payments import build_gate
        except ImportError:
            from mcp_payments import build_gate
        _GATE = build_gate(
            pay_to=args.x402_pay_to, price=args.x402_price,
            network=args.x402_network, asset=args.x402_asset,
            decimals=args.x402_decimals, facilitator_url=args.x402_facilitator,
        )
        print(f"[mcp] Phase 2 payments ENABLED — {args.x402_price} per call to {args.x402_pay_to}")
    else:
        print("[mcp] Phase 1 — tools are free (no --x402-pay-to)")

    print(f"[mcp] quickbeam MCP → {API_URL} | corpus={CORPUS} | transport={args.transport}")
    transport = "http" if args.transport == "streamable-http" else args.transport
    if transport in ("http", "sse"):
        mcp.run(transport=transport, host=args.host, port=args.port)
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
