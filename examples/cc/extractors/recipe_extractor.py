import json
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from cmoncrawl.processor.pipeline.extractor import BaseExtractor
from cmoncrawl.common.types import PipeMetadata

_BLOCK_MARKERS = (
    "access denied", "request unsuccessful",
    "pardon our interruption", "just a moment",
    "enable javascript"
)

_RECIPE_LIKE_TYPES = {"Recipe"}
_FALLBACK_RECIPE_HINTS = (
    "recipe", "ingredients", "instructions", "method", "cook", "bake"
)

class HighRecallRecipeExtractor(BaseExtractor):

    def extract_soup(self, soup: BeautifulSoup, metadata: PipeMetadata):
        if soup is None:
            return None

        url = metadata.domain_record.url
        domain = urlparse(url).netloc.lower().lstrip("www.")

        # Scaled-back block marker evaluation (first 1000 characters to prevent false positives)
        page_text_start = soup.get_text(" ", strip=True)[:1000].lower()
        if any(m in page_text_start for m in _BLOCK_MARKERS):
            return None

        recs = []

        # ─────────────────────────────────────────────
        # 1. COLLECT ALL JSON-LD NODES
        # ─────────────────────────────────────────────
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except Exception:
                continue

            for node in self._iter_nodes(data):
                if isinstance(node, dict):
                    rec = self._extract_recipe_from_node(node, url, domain)
                    if rec:
                        recs.append(rec)
        # ─────────────────────────────────────────────
        # 3. SAFE RETURN FOR CMONCRAWL PIPELINE
        # ─────────────────────────────────────────────
        if not recs:
            return None
            
        if len(recs) == 1:
            return recs[0] # Return the single dict
            
        # If multiple recipes are found, wrap them in a single dict 
        # so the pipeline writer doesn't crash on a list type.
        return {
            "url": url,
            "domain": domain,
            "type": "RecipeCollection",
            "recipes": recs
        }

    def _extract_recipe_from_node(self, node, url, domain):
        t = self._type_of(node)

        is_recipe = (
            t in _RECIPE_LIKE_TYPES
            or (isinstance(node.get("@type"), list) and "Recipe" in node.get("@type"))
        )

        ingredients = self._str_list(
            node.get("recipeIngredient") or node.get("ingredients")
        )
        instructions = node.get("recipeInstructions")

        if not (is_recipe or ingredients or instructions):
            return None

        rec = {
            "url": url,
            "domain": domain,
            "type": "Recipe" if is_recipe else "UnknownRecipeLike",
        }

        rec["title"] = self._first_str(node.get("name"), node.get("headline"))
        if ingredients:
            rec["ingredients"] = ingredients

        rec["cuisine"] = self._first_str(node.get("recipeCuisine"))
        rec["category"] = self._first_str(node.get("recipeCategory"))
        rec["yield"] = self._first_str(node.get("recipeYield"))
        rec["totalTime"] = self._first_str(node.get("totalTime"))
        rec["cookTime"] = self._first_str(node.get("cookTime"))
        rec["prepTime"] = self._first_str(node.get("prepTime"))
        rec["rating"] = self._rating(node.get("aggregateRating"))
        rec["ratingCount"] = self._rating_count(node.get("aggregateRating"))
        rec["nutrition"] = self._nutrition(node.get("nutrition"))
        rec["tags"] = self._keywords(node.get("keywords"))

        if not rec.get("title"):
            rec["title"] = self._extract_any_title(node)

        return self._clean(rec)

    def _looks_recipe_like(self, text: str) -> bool:
        t = text.lower()
        return sum(h in t for h in _FALLBACK_RECIPE_HINTS) >= 2

    def _heuristic_recipe(self, soup, url, domain):
        return {
            "url": url,
            "domain": domain,
            "type": "HeuristicRecipe",
            "title": self._title(soup),
        }

    @staticmethod
    def _iter_nodes(data):
        """Fixed node traversal generator preventing parent-graph yield duplication."""
        if isinstance(data, list):
            for x in data:
                yield from HighRecallRecipeExtractor._iter_nodes(x)
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                yield from HighRecallRecipeExtractor._iter_nodes(data["@graph"])
            else:
                yield data

    @staticmethod
    def _type_of(node):
        t = node.get("@type")
        if isinstance(t, list):
            return "Recipe" if "Recipe" in t else t[0]
        return t

    def _clean(self, rec):
        return {k: v for k, v in rec.items() if v not in (None, "", [], {})}

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
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [v.strip()]
        return []

    @staticmethod
    def _keywords(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if x]
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return []

    @staticmethod
    def _rating(v):
        if isinstance(v, dict):
            try:
                return float(v.get("ratingValue"))
            except Exception:
                return None
        return None

    @staticmethod
    def _rating_count(v):
        if isinstance(v, dict):
            for k in ("ratingCount", "reviewCount"):
                if k in v:
                    try:
                        return int(v[k])
                    except Exception:
                        pass
        return None

    @staticmethod
    def _nutrition(v):
        if not isinstance(v, dict):
            return {}
        keys = (
            "calories", "proteinContent", "fatContent",
            "carbohydrateContent", "sugarContent", "fiberContent"
        )
        return {k: v[k] for k in keys if k in v}

    def _extract_any_title(self, node):
        return node.get("name") or node.get("headline") or None

    @staticmethod
    def _title(soup):
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        h1 = soup.find("h1")
        return h1.get_text(" ", strip=True) if h1 else None

extractor = HighRecallRecipeExtractor()