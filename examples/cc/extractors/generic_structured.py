"""
Generic, domain-agnostic structured-data extractor for CmonCrawl.

Works on *any* page by harvesting the structured signals most of the web already
publishes, so heterogeneous pages across many domains normalize into one uniform
record shape that embeds and cross-domain-searches well:

  1. JSON-LD (schema.org) — Recipe, Article/NewsArticle, Product, JobPosting,
     Event, etc. The richest, most consistent source; we pick the "primary" node.
  2. OpenGraph / Twitter / standard <meta> — title, description, type, image,
     site name, published time, author. Near-universal fallback.

We deliberately store ONLY these structured signals — the facts a site already
publishes about itself for redistribution (OpenGraph/JSON-LD exist precisely so
others can re-surface them). We do NOT scrape the page's main body text: copying
the article/instructions/commentary into our store is the real copyright-exposure
surface, and it isn't needed — search runs over the structured facts, and results
link back to the source `url`. A page with no structured data is skipped, so the
output stays a database of facts, not a mirror of page content.

Field names (`title`, `description`, `tags`, `author`, …) are chosen so
quickbeam's role inference lights up title/subtitle/tags automatically.

Use a catch-all route (regex ".*") to send every crawled page here, regardless of
domain. Pages that are bot-block stubs or have no structured data return None.
"""

import json
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from cmoncrawl.processor.pipeline.extractor import BaseExtractor
from cmoncrawl.common.types import PipeMetadata

_BLOCK_MARKERS = (
    "Please contact the site owner for access",
    "VikingCloud", "Access Denied", "Request unsuccessful",
    "Pardon Our Interruption", "Just a moment...", "captcha",
)

# schema.org @types worth surfacing as the record's `type`, richest first. Generic
# container types (WebSite, Organization, BreadcrumbList) are skipped as "primary".
_PRIMARY_TYPES = (
    "Recipe", "NewsArticle", "Article", "BlogPosting", "Product", "JobPosting",
    "Event", "Movie", "TVSeries", "Book", "Course", "Place", "LocalBusiness",
    "Question", "HowTo", "Review", "Person", "VideoObject", "PodcastEpisode",
)
_SKIP_TYPES = {"WebSite", "WebPage", "Organization", "BreadcrumbList",
               "SearchAction", "SiteNavigationElement", "ImageObject"}

# Cap the publisher-supplied summary so a `description` can't smuggle in the whole
# article body. Real OpenGraph/JSON-LD descriptions are short; anything longer is a
# sign the field was stuffed with page content, so we trim it to a summary length.
_MAX_DESCRIPTION = 500


class GenericStructuredExtractor(BaseExtractor):
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

        # OpenGraph / meta fallbacks (don't overwrite better JSON-LD values).
        # Each og:/meta signal we find counts as structured data the site published.
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
            rec["datePublished"] = self._meta(soup, "article:published_time")
        if not rec.get("tags"):
            tags = self._meta(soup, "keywords") or self._meta(soup, "article:tag")
            if tags:
                rec["tags"] = [t.strip() for t in tags.split(",") if t.strip()]

        # Trim the publisher-supplied summary to a summary length; never store body text.
        if rec.get("description"):
            rec["description"] = rec["description"][:_MAX_DESCRIPTION]

        # Drop empties. Only emit pages that actually published structured facts and
        # carry a title — no structured data means nothing defensible to store.
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
                yield from GenericStructuredExtractor._iter_nodes(it)
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                yield from GenericStructuredExtractor._iter_nodes(data["@graph"])
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
        if t == "Recipe":
            # Ingredient lists are short factual lists (not the creative expression);
            # recipeInstructions are deliberately NOT stored — that's the copyrightable
            # part. Everything else here is factual metadata the site self-publishes.
            out["ingredients"] = self._str_list(node.get("recipeIngredient")
                                                or node.get("ingredients"))
            out["cuisine"] = self._first_str(node.get("recipeCuisine"))
            out["recipeCategory"] = self._first_str(node.get("recipeCategory"))
            out["prepTime"] = self._first_str(node.get("prepTime"))
            out["cookTime"] = self._first_str(node.get("cookTime"))
            out["totalTime"] = self._first_str(node.get("totalTime"))
            out["recipeYield"] = self._first_str(node.get("recipeYield"))
            out["suitableForDiet"] = self._str_list(node.get("suitableForDiet"))
            out["nutrition"] = self._nutrition(node.get("nutrition"))
            out["rating"] = self._rating(node.get("aggregateRating"))
            out["ratingCount"] = self._rating_count(node.get("aggregateRating"))
        elif t == "Product":
            brand = node.get("brand")
            out["brand"] = brand.get("name") if isinstance(brand, dict) else brand
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                out["price"] = offers.get("price")
                out["priceCurrency"] = offers.get("priceCurrency")
            out["rating"] = self._rating(node.get("aggregateRating"))
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
    def _rating(v):
        if isinstance(v, dict):
            r = v.get("ratingValue")
            return float(r) if isinstance(r, (int, float, str)) and str(r).replace(".", "", 1).isdigit() else None
        return None

    @staticmethod
    def _rating_count(v):
        if isinstance(v, dict):
            c = v.get("ratingCount") or v.get("reviewCount")
            return int(c) if isinstance(c, (int, float, str)) and str(c).isdigit() else None
        return None

    @staticmethod
    def _nutrition(v):
        # schema.org NutritionInformation: factual per-serving values. Keep the
        # common factual fields; skip anything free-text.
        if not isinstance(v, dict):
            return {}
        keys = ("calories", "servingSize", "fatContent", "saturatedFatContent",
                "carbohydrateContent", "sugarContent", "fiberContent",
                "proteinContent", "sodiumContent", "cholesterolContent")
        out = {}
        for k in keys:
            val = v.get(k)
            if isinstance(val, (str, int, float)) and str(val).strip():
                out[k] = str(val).strip()
        return out

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


extractor = GenericStructuredExtractor()
