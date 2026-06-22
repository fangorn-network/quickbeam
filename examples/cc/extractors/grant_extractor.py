"""
Structured-data extractor for CmonCrawl optimized for Grant & Funding Opportunities.

Harvests structured signals (JSON-LD and Meta fields) to isolate open funding opportunities, 
RFPs, and grants, normalizing them into a uniform shape for semantic embedding.
"""

import json
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from cmoncrawl.processor.pipeline.extractor import BaseExtractor
from cmoncrawl.common.types import PipeMetadata

_BLOCK_MARKERS = (
    "Please contact the site owner for access", "captcha",
    "Expired Round", "Closed for Applications", "Submissions closed", 
    "No longer accepting applications", "Deadline passed", 
    "Applications are closed", "Funding exhausted"
)

# Prioritize schema.org types matching funding frameworks and documentation
_PRIMARY_TYPES = (
    "Grant", "FundingScheme", "FinancialProduct", "NewsArticle", 
    "Article", "BlogPosting", "JobPosting", "Event", "HowTo"
)
_SKIP_TYPES = {"WebSite", "WebPage", "Organization", "BreadcrumbList",
               "SearchAction", "SiteNavigationElement", "ImageObject"}

_MAX_DESCRIPTION = 500


class GrantStructuredExtractor(BaseExtractor):
    def extract_soup(self, soup: BeautifulSoup, metadata: PipeMetadata):
        url = metadata.domain_record.url
        if soup is None:
            return None
        text = soup.get_text(" ", strip=True)
        if not text or len(text) < 200 or any(m in text for m in _BLOCK_MARKERS):
            return None

        rec: dict = {"url": url, "domain": urlparse(url).netloc.lower().lstrip("www.")}

        node = self._primary_jsonld(soup)
        have_structured = node is not None
        if node:
            rec["type"] = self._type_of(node)
            rec["title"] = self._first_str(node.get("name"), node.get("headline"))
            rec["description"] = self._first_str(node.get("description"))
            rec["author"] = self._person_name(node.get("author"))
            rec["datePublished"] = self._first_str(node.get("datePublished"),
                                                   node.get("uploadDate"))
            rec["tags"] = self._keywords(node.get("keywords"))
            rec.update(self._type_specific(node))

        # OpenGraph / Meta fallbacks (specifically tuned to capture programmatic funding targets)
        og_type = self._meta(soup, "og:type")
        if og_type:
            rec.setdefault("type", og_type)
            have_structured = True
            
        if not rec.get("title"):
            og_title = self._meta(soup, "og:title")
            if og_title:
                have_structured = True
            rec["title"] = og_title or self._title(soup)
            
        if not rec.get("description"):
            og_desc = self._meta(soup, "og:description") or self._meta(soup, "description")
            if og_desc:
                rec["description"] = og_desc
                have_structured = True
                
        site = self._meta(soup, "og:site_name")
        if site:
            rec.setdefault("siteName", site)
            have_structured = True
            
        image = self._meta(soup, "og:image")
        if image:
            rec.setdefault("image", image)
            have_structured = True
            
        if not rec.get("datePublished"):
            rec["datePublished"] = (
                self._meta(soup, "article:published_time") or 
                self._meta(soup, "grant:published_time")
            )
            
        # Extract custom funding fields from meta fallbacks if missing from JSON-LD
        if not rec.get("amount"):
            rec["amount"] = self._meta(soup, "grant:amount") or self._meta(soup, "funding:amount")
        if not rec.get("deadline"):
            rec["deadline"] = self._meta(soup, "grant:deadline") or self._meta(soup, "funding:expires")

        if not rec.get("tags"):
            tags = self._meta(soup, "keywords") or self._meta(soup, "article:tag")
            if tags:
                rec["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

        # Trim description payload to avoid smuggling full body text
        if rec.get("description"):
            rec["description"] = rec["description"][:_MAX_DESCRIPTION]

        rec = {k: v for k, v in rec.items() if v not in (None, "", [], {})}
        if not have_structured or not rec.get("title"):
            return None
        return rec

    # ── JSON-LD selection ─────────────────────────────────────────────────────
    def _primary_jsonld(self, soup: BeautifulSoup):
        candidates = []
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            for node in self._iter_nodes(data):
                if isinstance(node, dict):
                    candidates.append(node)
        if not candidates:
            return None

        def rank(n):
            t = self._type_of(n)
            return _PRIMARY_TYPES.index(t) if t in _PRIMARY_TYPES else len(_PRIMARY_TYPES) + 1

        ranked = sorted(
            (n for n in candidates if self._type_of(n) not in _SKIP_TYPES),
            key=rank,
        )
        return ranked[0] if ranked else None

    @staticmethod
    def _iter_nodes(data):
        if isinstance(data, list):
            for it in data:
                yield from GrantStructuredExtractor._iter_nodes(it)
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                yield from GrantStructuredExtractor._iter_nodes(data["@graph"])
            yield data

    @staticmethod
    def _type_of(node: dict):
        t = node.get("@type")
        if isinstance(t, list):
            for cand in _PRIMARY_TYPES:
                if cand in t:
                    return cand
            return t[0] if t else None
        return t

    # ── type-specific enrichment ──────────────────────────────────────────────
    def _type_specific(self, node: dict) -> dict:
        t = self._type_of(node)
        out: dict = {}
        
        # Funding & Grant Specific targets
        if t in ("Grant", "FundingScheme", "FinancialProduct", "NewsArticle", "Article"):
            # Normalize variations of maximum capital allocation or structural ranges
            out["amount"] = self._first_str(
                node.get("amount"), 
                node.get("fundingAmount"), 
                node.get("awardCeiling"),
                node.get("value")
            )
            # Pull program close windows, lifecycle expirations, or submission cutoffs
            out["deadline"] = self._first_str(
                node.get("validUntil"), 
                node.get("expires"), 
                node.get("endDate"),
                node.get("closedDate")
            )
            # Map the entity dispensing capital or deploying the program
            sponsor = node.get("sponsor") or node.get("provider") or node.get("funder")
            out["funder"] = sponsor.get("name") if isinstance(sponsor, dict) else self._first_str(sponsor)
            
            # Isolate regional limitations or eligibility groupings
            out["eligibility"] = self._str_list(
                node.get("eligibility") or 
                node.get("targetAudience") or 
                node.get("eligibleRegion")
            )
            out["grantCategory"] = self._first_str(node.get("category"), node.get("fundingType"))

        elif t == "JobPosting":
            org = node.get("hiringOrganization")
            out["company"] = org.get("name") if isinstance(org, dict) else org
            loc = node.get("jobLocation")
            if isinstance(loc, dict):
                addr = loc.get("address")
                if isinstance(addr, dict):
                    out["location"] = self._first_str(addr.get("addressLocality"),
                                                      addr.get("addressRegion"))
            out["employmentType"] = self._first_str(node.get("employmentType"))
            
        elif t in ("Event",):
            out["startDate"] = self._first_str(node.get("startDate"))
            loc = node.get("location")
            if isinstance(loc, dict):
                out["location"] = self._first_str(loc.get("name"))
                
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}

    # ── small helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _first_str(*vals):
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, list) and v and isinstance(v[0], str):
                return v[0].strip()
        return None

    @staticmethod
    def _str_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    @staticmethod
    def _keywords(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [t.strip() for t in v.split(",") if t.strip()]
        return []

    @staticmethod
    def _person_name(v):
        if isinstance(v, dict):
            return v.get("name")
        if isinstance(v, list) and v:
            first = v[0]
            return first.get("name") if isinstance(first, dict) else (first if isinstance(first, str) else None)
        return v if isinstance(v, str) else None

    @staticmethod
    def _meta(soup, prop):
        tag = (soup.find("meta", property=prop)
               or soup.find("meta", attrs={"name": prop}))
        val = tag.get("content") if tag else None
        return val.strip() if isinstance(val, str) and val.strip() else None

    @staticmethod
    def _title(soup):
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        h1 = soup.find("h1")
        return h1.get_text(" ", strip=True) if h1 else None


extractor = GrantStructuredExtractor()