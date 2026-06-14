"""
roles.py — schema-agnostic semantic role inference.

The app used to hardcode "title is `title`, tags are `genres/moods/themes/contexts`,
the artist is `byArtist`, the playable handle is `platformId`". This module recovers
that interpretation from the data itself, so *any* schema works with no per-domain
config.

Given a sample of flattened records (field -> value) and optionally the schema's
declared field types, `infer_roles` assigns each field to a semantic ROLE:

    identity  — stable key for a record           (singular)
    title     — primary display label             (singular)
    subtitle  — byline / neighborhood entity      (singular)
    temporal  — a date/time                       (singular)
    spatial   — a geographic anchor               (singular)
    media     — a playable/openable handle        (singular; gates playback)
    tags      — array-of-string facets            (multi; filters + taste kernel)
    measures  — numeric scalars                   (multi)
    relations — grouping references               (multi)
    text      — long free text                    (multi)

The output is a plain dict (JSON-serializable) so the server can hand it to the
renderer over `GET /schema` and both sides interpret records identically.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# SPEC — synonym tables + types. Keep this the single source of truth; the
# TypeScript twin in the renderer mirrors it exactly.
# ---------------------------------------------------------------------------

# Each list is matched against the *normalized* field name (lowercased,
# non-alphanumerics stripped). Exact match scores strong; substring scores weak.
ROLE_SYNONYMS: dict[str, list[str]] = {
    "identity":  ["id", "trackid", "changesetid", "contentid", "uid", "guid",
                  "key", "recordid", "hash", "leaf", "nullifier"],
    "title":     ["title", "name", "label", "headline", "subject"],
    "subtitle":  ["byartist", "artist", "author", "creator", "user", "userid",
                  "owner", "by", "publisher", "maker", "contributor", "channel"],
    "tags":      ["genres", "genre", "moods", "mood", "themes", "theme",
                  "contexts", "context", "tags", "tag", "categories", "category",
                  "keywords", "topics", "labels"],
    "temporal":  ["datepublished", "date", "createdat", "created", "timestamp",
                  "time", "datetime", "publishedat", "updatedat", "year",
                  "modified"],
    "spatial":   ["bbox", "boundingbox", "bounds", "geo", "location",
                  "coordinates", "latlon", "lat", "lon", "lng", "latitude",
                  "longitude", "point", "geometry", "place"],
    "measures":  ["duration", "durationms", "length", "count", "numchanges",
                  "size", "score", "amount", "quantity", "votes", "plays",
                  "views", "rank", "weight"],
    "media":     ["audio", "media", "url", "uri", "handle", "src", "source",
                  "platformid", "videoid", "contenturl", "stream", "embed",
                  "file", "asset"],
    "relations": ["album", "albumname", "release", "releasegroup", "group",
                  "collection", "parent", "set", "series", "playlist"],
    "text":      ["description", "comment", "bio", "notes", "body", "content",
                  "abstract", "review", "summary", "message"],
}

SINGULAR_ROLES = ["identity", "title", "subtitle", "temporal", "spatial", "media"]
MULTI_ROLES    = ["tags", "measures", "relations", "text"]
# Priority for breaking ties when a field could fill several singular roles.
SINGULAR_PRIORITY = ["identity", "media", "spatial", "temporal", "title", "subtitle"]

# Envelope / bookkeeping fields that are never a semantic role.
DENYLIST = {
    "schemaversion", "mbid", "_mbid", "datacid", "manifestcid", "namehash",
    "blocktimestamp", "version", "owner",  # owner handled as subtitle fallback only
}

STRONG = 3.0   # exact normalized name match
WEAK   = 1.5   # substring name match
THRESHOLD = 1.5

_LONG_TEXT_CHARS = 120
_DATE_RE = re.compile(r"^\s*\d{4}(-\d{2}(-\d{2})?)?([T ]\d{2}:\d{2})?")
_GEO_KEYS = {"lat", "lon", "lng", "latitude", "longitude",
             "min_lon", "min_lat", "max_lon", "max_lat", "x", "y"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _name_score(norm_name: str, role: str) -> float:
    # Substring matching is only safe for reasonably long tokens — otherwise
    # "lat" matches "platform", "id" matches everything, etc. Exact matches are
    # always honored; substrings require >= 4 chars on both sides.
    best = 0.0
    for syn in ROLE_SYNONYMS[role]:
        if norm_name == syn:
            return STRONG
        if len(syn) >= 4 and len(norm_name) >= 4 and (syn in norm_name or norm_name in syn):
            best = max(best, WEAK)
    return best


# ---------------------------------------------------------------------------
# VALUE-SHAPE INSPECTION — aggregate each field's observed type across records.
# ---------------------------------------------------------------------------

def _looks_date(v) -> bool:
    if isinstance(v, str) and _DATE_RE.match(v):
        return True
    return False


def _looks_geo(v) -> bool:
    if isinstance(v, dict):
        keys = {str(k).lower() for k in v.keys()}
        return bool(keys & _GEO_KEYS)
    return False


def _looks_url(v) -> bool:
    return isinstance(v, str) and bool(re.match(r"^(https?:|ipfs:|spotify:|[a-z]+://)", v.strip(), re.I))


def _field_shape(values: list) -> dict:
    """Reduce observed values for one field to type flags."""
    n = len(values)
    if n == 0:
        return {"array_of_str": False, "number": False, "date": False,
                "geo": False, "url": False, "long_text": False, "string": False}
    array_str = num = date = geo = url = string = 0
    total_len = 0
    str_count = 0
    for v in values:
        if isinstance(v, list):
            if v and all(isinstance(x, str) for x in v):
                array_str += 1
        elif isinstance(v, bool):
            pass  # ignore booleans for measure inference
        elif isinstance(v, (int, float)):
            num += 1
        elif isinstance(v, str):
            string += 1
            str_count += 1
            total_len += len(v)
            if _looks_date(v):
                date += 1
            if _looks_url(v):
                url += 1
        elif isinstance(v, dict):
            if _looks_geo(v):
                geo += 1
    avg_len = (total_len / str_count) if str_count else 0
    half = n / 2
    return {
        "array_of_str": array_str >= half and array_str > 0,
        "number":       num >= half and num > 0,
        "date":         date >= half and date > 0,
        "geo":          geo >= half and geo > 0,
        "url":          url >= half and url > 0,
        "string":       string >= half and string > 0,
        "long_text":    avg_len >= _LONG_TEXT_CHARS,
    }


_TYPE_BONUS = {
    "tags":     ("array_of_str", 2.0),
    "measures": ("number",       1.5),
    "temporal": ("date",         2.0),
    "spatial":  ("geo",          2.5),
    "media":    ("url",          1.5),
    "text":     ("long_text",    2.0),
}


def _role_scores(norm_name: str, shape: dict) -> dict[str, float]:
    scores: dict[str, float] = {}
    for role in ROLE_SYNONYMS:
        s = _name_score(norm_name, role)
        bonus = _TYPE_BONUS.get(role)
        if bonus and shape.get(bonus[0]):
            s += bonus[1]
        # identity/title/subtitle are stringy — small nudge for short strings
        if role in ("identity", "title", "subtitle") and shape.get("string") and not shape.get("long_text"):
            s += 0.5
        if s > 0:
            scores[role] = s
    return scores


# ---------------------------------------------------------------------------
# LABELS — turn a field name into a human label for the chosen role.
# ---------------------------------------------------------------------------

_SINGULARIZE = [("ies", "y"), ("ses", "s"), ("s", "")]


def field_label(name: str, singular: bool = False) -> str:
    """`byArtist` -> "Artist", `user_id` -> "User", `num_changes` -> "Num Changes"."""
    if not name:
        return ""
    raw = re.sub(r"^by(?=[A-Z_])", "", name)            # drop a leading "by"
    raw = re.sub(r"(_id|Id)$", "", raw) or raw          # drop trailing id
    parts = re.sub(r"[_\-]+", " ", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)).split()
    if not parts:
        parts = [name]
    if singular:
        last = parts[-1].lower()
        for suf, rep in _SINGULARIZE:
            if last.endswith(suf) and len(last) > len(suf) + 1:
                parts[-1] = parts[-1][: len(parts[-1]) - len(suf)] + rep
                break
    return " ".join(w[:1].upper() + w[1:] for w in parts)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def infer_roles(records: list[dict], fields_spec: dict[str, str] | None = None) -> dict:
    """
    records      — list of flattened field dicts (field name -> value).
    fields_spec  — optional {field_name: declared_type} from the schema registry,
                   used to seed fields that never appeared with a value.

    Returns a JSON-serializable RoleMap.
    """
    # 1. Collect the universe of field names and a sample of values per field.
    values_by_field: dict[str, list] = {}
    for rec in records[:500]:
        if not isinstance(rec, dict):
            continue
        for k, v in rec.items():
            if v is None:
                continue
            values_by_field.setdefault(k, [])
            if len(values_by_field[k]) < 50:
                values_by_field[k].append(v)
    for name in (fields_spec or {}):
        values_by_field.setdefault(name, [])

    fields = sorted(values_by_field.keys())

    # 2. Score every field for every role.
    shapes: dict[str, dict] = {}
    scores: dict[str, dict[str, float]] = {}
    for f in fields:
        if _norm(f) in DENYLIST:
            continue
        shape = _field_shape(values_by_field[f])
        shapes[f] = shape
        scores[f] = _role_scores(_norm(f), shape)

    role_map: dict = {r: None for r in SINGULAR_ROLES}
    for r in MULTI_ROLES:
        role_map[r] = []
    used: set[str] = set()

    # 3. Tags first — array-of-string facets are unambiguous and load-bearing.
    for f in fields:
        if f in used or f not in scores:
            continue
        if shapes[f].get("array_of_str") and scores[f].get("tags", 0) >= THRESHOLD:
            role_map["tags"].append(f)
            used.add(f)
    # Order tag fields by vocabulary richness so the most informative facet
    # (e.g. genres) is primary — this drives map coloring + the "primary tag".
    def _distinct(field: str) -> int:
        seen = set()
        for v in values_by_field.get(field, []):
            if isinstance(v, list):
                seen.update(str(x) for x in v)
        return len(seen)
    role_map["tags"].sort(key=lambda f: (-_distinct(f), f))

    # 4. Singular roles, in priority order, each claiming its best free field.
    for role in SINGULAR_PRIORITY:
        best_f, best_s = None, THRESHOLD - 0.001
        for f in fields:
            if f in used or f not in scores:
                continue
            s = scores[f].get(role, 0)
            if s > best_s:
                best_f, best_s = f, s
        if best_f is not None:
            role_map[role] = best_f
            used.add(best_f)

    # 5. Multi roles over the remainder.
    for f in fields:
        if f in used or f not in scores:
            continue
        sc = scores[f]
        if shapes[f].get("number") and sc.get("measures", 0) >= THRESHOLD:
            role_map["measures"].append(f); used.add(f); continue
        if sc.get("relations", 0) >= STRONG:
            role_map["relations"].append(f); used.add(f); continue
        if shapes[f].get("long_text") or sc.get("text", 0) >= STRONG:
            role_map["text"].append(f); used.add(f); continue

    # 6. Labels for the assigned fields.
    labels: dict[str, str] = {}
    for role in SINGULAR_ROLES:
        if role_map[role]:
            labels[role] = field_label(role_map[role], singular=True)
    for role in MULTI_ROLES:
        if role_map[role]:
            labels[role] = ", ".join(field_label(f) for f in role_map[role])

    role_map["labels"] = labels
    role_map["fields"] = fields
    return role_map
