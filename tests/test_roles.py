"""
Tests for semantic role inference + the embed-collapse guard (roles.py).

The headline regression: a record with a literal `text` blurb must get that blurb
into its embedded document text. Two bugs previously prevented it —
  1. `"text"` was missing from the `text`-role synonyms, and a short blurb
     (< 120 chars) also failed the `long_text` heuristic → the field was dropped.
  2. a stale `./db/role_map.json` from a *different* corpus was applied verbatim,
     so every record embedded the same empty `"Title: . Tags:"` string and all
     vectors collapsed to one point (identical, undiscriminating search scores).
`role_map_applies` guards (2); the synonym fix addresses (1).

Run:  ./venv/bin/python -m pytest tests/test_roles.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quickbeam.roles import infer_roles, role_map_applies
from quickbeam.embeddings import compose_document_text

# A robinhood-shaped Asset sample: scalar tags, a rich (but short) `text` blurb.
_ASSETS = [
    {"entityType": "Asset", "symbol": "NVDA", "name": "NVIDIA", "sector": "Semiconductors",
     "price": 900.1, "holders": 42,
     "text": "NVIDIA (NVDA) is a tokenized technology stock. Designs GPUs for AI."},
    {"entityType": "Asset", "symbol": "COIN", "name": "Coinbase", "sector": "Crypto",
     "price": 165.6, "holders": 24,
     "text": "Coinbase (COIN) is a tokenized crypto stock. Largest US exchange."},
    {"entityType": "Asset", "symbol": "GME", "name": "GameStop", "sector": "Retail",
     "price": 22.8, "holders": 6,
     "text": "GameStop (GME) is a tokenized equity stock. A well-known meme stock."},
]

# The stale map that caused the collapse: a places/venues role map. None of its
# fields exist on an Asset record.
_STALE_PLACES_MAP = {
    "title": "title", "subtitle": "author", "text": ["body"],
    "tags": ["reviews", "categories", "localities"], "measures": ["rating"],
    "fields": ["title", "author", "body", "reviews"],
}


# ---------------------------------------------------------------------------
# Inference — the `text` blurb is recovered as the text role.
# ---------------------------------------------------------------------------
def test_literal_text_field_is_recognized_as_text_role():
    rm = infer_roles(_ASSETS)
    assert rm["text"] == ["text"]          # was [] before the synonym fix
    assert rm["title"] == "name"           # a real, varying display label


def test_short_blurb_still_captured_despite_long_text_heuristic():
    # Every blurb here is < 120 chars, so the length heuristic alone would miss it;
    # the exact `text` name-synonym is what saves it.
    assert all(len(a["text"]) < 120 for a in _ASSETS)
    assert infer_roles(_ASSETS)["text"] == ["text"]


# ---------------------------------------------------------------------------
# Stale-map guard.
# ---------------------------------------------------------------------------
def test_guard_rejects_foreign_map():
    assert role_map_applies(_STALE_PLACES_MAP, _ASSETS) is False


def test_guard_accepts_matching_map():
    assert role_map_applies(infer_roles(_ASSETS), _ASSETS) is True


def test_guard_rejects_empty():
    assert role_map_applies({}, _ASSETS) is False
    assert role_map_applies({"title": None, "tags": [], "text": []}, _ASSETS) is False


# ---------------------------------------------------------------------------
# Composer — the whole point: distinct, blurb-bearing document text.
# ---------------------------------------------------------------------------
def test_composer_includes_blurb_and_differs_per_record():
    rm = infer_roles(_ASSETS)
    texts = [compose_document_text(a, rm) for a in _ASSETS]
    assert all(t.startswith("search_document: ") for t in texts)
    # The rich blurb is embedded, so the docs are distinct (no collapse).
    assert "Designs GPUs for AI" in texts[0]
    assert "Largest US exchange" in texts[1]
    assert len(set(texts)) == len(texts)


def test_stale_map_would_collapse_documents():
    # Demonstrates the failure the guard prevents: under the foreign map, every
    # Asset composes the identical empty string.
    texts = [compose_document_text(a, _STALE_PLACES_MAP) for a in _ASSETS]
    assert len(set(texts)) == 1                       # every Asset → same string
    assert texts[0].startswith("search_document: Title: . Tags:")
    assert "GPUs" not in texts[0]                     # the blurb is absent
