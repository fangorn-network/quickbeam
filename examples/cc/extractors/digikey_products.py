"""
DigiKey product-page extractor for CmonCrawl.

Parses a DigiKey product detail page (e.g.
https://www.digikey.com/en/products/detail/<mfr>/<mpn>/<id>) into a structured
record. It tries the most reliable sources first and falls back gracefully:

  1. JSON-LD  <script type="application/ld+json">  — canonical Product schema
     (name, mpn, brand, category, offers/price). Most stable across redesigns.
  2. __NEXT_DATA__  — DigiKey's Next.js payload; a best-effort recursive hunt for
     product-ish keys when no JSON-LD is present.
  3. Meta tags + the on-page spec table — title/description from og: meta, and the
     attribute label→value table (Series, Package, Voltage - DC Reverse, …).

NOTE: Common Crawl currently captures DigiKey product URLs as bot-block stubs
("Please contact the site owner for access"), so this extractor returns None for
those. It is meant for real product HTML — validate it against a non-blocked
source (live fetch, or a crawl that captured full pages).
"""

import json
import re

from bs4 import BeautifulSoup
from cmoncrawl.processor.pipeline.extractor import BaseExtractor
from cmoncrawl.common.types import PipeMetadata

# Substrings that mark a bot-block / empty capture — skip these outright.
_BLOCK_MARKERS = (
    "Please contact the site owner for access",
    "VikingCloud",
    "Access Denied",
    "Request unsuccessful",
    "Pardon Our Interruption",
)

# Spec labels DigiKey renders on a product page. Used to pull values out of the
# attribute table by label, tolerant of the exact surrounding markup.
_SPEC_LABELS = (
    "Manufacturer", "Manufacturer Product Number", "Series", "Package",
    "Product Status", "Technology", "Voltage - DC Reverse (Vr) (Max)",
    "Current - Average Rectified (Io)", "Voltage - Forward (Vf) (Max) @ If",
    "Speed", "Current - Reverse Leakage @ Vr", "Capacitance @ Vr, F",
    "Mounting Type", "Package / Case", "Supplier Device Package",
    "Operating Temperature - Junction", "Detailed Description", "Description",
)


class DigiKeyProductExtractor(BaseExtractor):
    def extract_soup(self, soup: BeautifulSoup, metadata: PipeMetadata):
        url = metadata.domain_record.url
        if soup is None:
            return None

        text = soup.get_text(" ", strip=True)
        if not text or len(text) < 200 or any(m in text for m in _BLOCK_MARKERS):
            return None  # bot-block stub / empty capture

        product: dict = {}
        product.update(self._from_jsonld(soup))
        if not product.get("mpn"):
            product.update({k: v for k, v in self._from_next_data(soup).items() if v})

        # Always enrich from meta + the visible spec table (don't overwrite better data).
        attrs = self._spec_table(soup)
        for key, src in (
            ("manufacturer", "Manufacturer"),
            ("mpn", "Manufacturer Product Number"),
            ("category", None),
            ("series", "Series"),
            ("package", "Package / Case"),
            ("productStatus", "Product Status"),
        ):
            if not product.get(key) and src and attrs.get(src):
                product[key] = attrs[src]

        product.setdefault("title", self._meta(soup, "og:title") or self._title(soup))
        product.setdefault("description",
                           attrs.get("Detailed Description")
                           or attrs.get("Description")
                           or self._meta(soup, "og:description"))
        product.setdefault("category", self._category(soup))
        if attrs:
            product["attributes"] = attrs

        product["url"] = url

        # Require at least an identifier or a title — otherwise it's not a product page.
        if not (product.get("mpn") or product.get("title")):
            return None
        return product

    # ── JSON-LD ───────────────────────────────────────────────────────────────
    def _from_jsonld(self, soup: BeautifulSoup) -> dict:
        out: dict = {}
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            for node in self._iter_jsonld_nodes(data):
                if self._jsonld_type(node) != "Product":
                    continue
                brand = node.get("brand")
                offers = node.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                out = {
                    "title": node.get("name"),
                    "mpn": node.get("mpn") or node.get("sku"),
                    "sku": node.get("sku"),
                    "gtin": node.get("gtin13") or node.get("gtin"),
                    "manufacturer": brand.get("name") if isinstance(brand, dict) else brand,
                    "category": node.get("category"),
                    "description": node.get("description"),
                }
                if isinstance(offers, dict):
                    out["price"] = offers.get("price")
                    out["priceCurrency"] = offers.get("priceCurrency")
                    avail = offers.get("availability")
                    if isinstance(avail, str):
                        out["availability"] = avail.rsplit("/", 1)[-1]
                return {k: v for k, v in out.items() if v not in (None, "")}
        return out

    @staticmethod
    def _iter_jsonld_nodes(data):
        if isinstance(data, list):
            for item in data:
                yield from DigiKeyProductExtractor._iter_jsonld_nodes(item)
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                yield from DigiKeyProductExtractor._iter_jsonld_nodes(data["@graph"])
            yield data

    @staticmethod
    def _jsonld_type(node: dict):
        t = node.get("@type")
        if isinstance(t, list):
            return "Product" if "Product" in t else (t[0] if t else None)
        return t

    # ── __NEXT_DATA__ (best effort) ───────────────────────────────────────────
    def _from_next_data(self, soup: BeautifulSoup) -> dict:
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag:
            return {}
        try:
            data = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, ValueError, TypeError):
            return {}
        # Hunt for the first dict carrying DigiKey product-ish keys.
        wanted = {
            "mpn": ("manufacturerProductNumber", "manufacturerPartNumber", "mpn"),
            "title": ("productName", "name"),
            "description": ("productDescription", "detailedDescription"),
            "manufacturer": ("manufacturerName",),
            "category": ("categoryName",),
        }
        for node in self._walk_dicts(data):
            hit = {}
            for field, keys in wanted.items():
                for k in keys:
                    v = node.get(k)
                    if isinstance(v, str) and v.strip():
                        hit[field] = v.strip()
                        break
            if hit.get("mpn") or (hit.get("title") and hit.get("manufacturer")):
                # manufacturer can be a nested {name|value}
                mfr = node.get("manufacturer")
                if isinstance(mfr, dict):
                    hit.setdefault("manufacturer", mfr.get("name") or mfr.get("value"))
                return hit
        return {}

    @staticmethod
    def _walk_dicts(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from DigiKeyProductExtractor._walk_dicts(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from DigiKeyProductExtractor._walk_dicts(v)

    # ── spec table / meta fallbacks ───────────────────────────────────────────
    def _spec_table(self, soup: BeautifulSoup) -> dict:
        """Pull label→value pairs for known DigiKey spec labels, tolerant of markup.

        Handles two common shapes: <tr><td>label</td><td>value</td></tr> and a
        <dl><dt>label</dt><dd>value</dd></dl>, plus a generic "label element whose
        next sibling cell holds the value" pass.
        """
        attrs: dict = {}
        labels = {label.lower(): label for label in _SPEC_LABELS}

        # <tr> rows
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(" ", strip=True)
                canon = labels.get(key.lower())
                if canon:
                    val = cells[1].get_text(" ", strip=True)
                    if val and val != "-":
                        attrs.setdefault(canon, val)

        # <dl> definition lists
        for dt in soup.find_all("dt"):
            key = dt.get_text(" ", strip=True)
            canon = labels.get(key.lower())
            dd = dt.find_next_sibling("dd")
            if canon and dd:
                val = dd.get_text(" ", strip=True)
                if val and val != "-":
                    attrs.setdefault(canon, val)

        return attrs

    @staticmethod
    def _meta(soup: BeautifulSoup, prop: str):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        val = tag.get("content") if tag else None
        return val.strip() if isinstance(val, str) and val.strip() else None

    @staticmethod
    def _title(soup: BeautifulSoup):
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        h1 = soup.find("h1")
        return h1.get_text(" ", strip=True) if h1 else None

    def _category(self, soup: BeautifulSoup):
        # DigiKey shows a breadcrumb like "Discrete Semiconductor Products | Single Diodes".
        crumb = soup.find(attrs={"itemtype": re.compile("BreadcrumbList", re.I)})
        if crumb:
            parts = [a.get_text(" ", strip=True) for a in crumb.find_all("a")]
            parts = [p for p in parts if p and p.lower() not in ("home", "products")]
            if parts:
                return " | ".join(parts[-2:])
        return None


extractor = DigiKeyProductExtractor()
