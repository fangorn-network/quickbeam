"""
Map CmonCrawl extract output → Fangorn `{name, fields}` records.

`cmon extract` writes JSONL files into its output directory: one JSON object per
line, being the dict the extractor returned (CmonCrawl may add provenance keys
such as `url`/`timestamp`). We read every output file, derive a stable record
`name`, and pass the object through as `fields`. Downstream role inference
(quickbeam/roles.py) discovers title/subtitle/tags/etc automatically — exactly as
for the OSM/MB pipelines — so no domain-specific mapping is needed here.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterator

# Fields an extractor commonly emits that make a stable, human-meaningful id.
_NAME_KEYS = ("url", "record_url", "permalink", "id", "name", "slug")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")

# Keys CmonCrawl's JSON streamer wraps around the extractor's output; not part of
# the extracted record itself, so we drop them before publishing.
_CMON_WRAPPER_KEYS = ("additional_info",)


def _slug(value: str, max_len: int = 96) -> str:
    s = _SLUG_RE.sub("-", value.strip()).strip("-")
    return s[:max_len] or "rec"


def _record_name(rec: dict, fallback_seed: str) -> str:
    for key in _NAME_KEYS:
        v = rec.get(key)
        if isinstance(v, str) and v.strip():
            return _slug(v)
    digest = hashlib.sha256(
        json.dumps(rec, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return f"{fallback_seed}-{digest}"


def iter_extracted(output_dir: str | Path) -> Iterator[dict]:
    """Yield each JSON object across all extract output files."""
    output_dir = Path(output_dir)
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        stripped = text.lstrip()
        if not stripped:
            continue
        # Support both JSONL (one object per line) and a single JSON array/object.
        if stripped[0] == "[":
            try:
                for obj in json.loads(text):
                    if isinstance(obj, dict):
                        yield obj
                continue
            except json.JSONDecodeError:
                pass
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def to_records(output_dir: str | Path, *, name_seed: str = "page") -> list[dict]:
    """
    Read every extracted object and return Fangorn `{name, fields}` records,
    de-duplicated by name (last write wins). `name_seed` prefixes hash-derived
    names when an extractor emits no natural id field.
    """
    by_name: dict[str, dict] = {}
    for rec in iter_extracted(output_dir):
        for k in _CMON_WRAPPER_KEYS:
            rec.pop(k, None)
        if not rec:
            continue
        name = _record_name(rec, name_seed)
        by_name[name] = {"name": name, "fields": rec}
    return list(by_name.values())
