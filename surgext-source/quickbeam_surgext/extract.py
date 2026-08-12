"""
PDF → raw records for the Surge XT user manual (the I/O + heuristics half).

The manual is a Chrome-printed PDF with a clean 4-level heading hierarchy expressed
purely by font size, so structure is recovered deterministically (no LLM, so the same
PDF always yields byte-identical records → Fangorn's content-addressing shares deltas):

    22.5pt → H1   18pt → H2   15pt → H3   13.5pt → H4   12pt → body   8pt → page footer

One quirk this file works around: the heading/table glyphs use per-ligature Type3 sub-fonts
that PyMuPDF's `find_tables().extract()` re-orders wrong ("distortion" → "distortoi n").
Plain text and clip-text are NOT affected, so we take table *structure* from `find_tables()`
but pull each cell's text via `get_text("text", clip=cell_rect)` (clean).

`extract(pdf_path)` returns a flat list of raw `kind`-tagged records; the pure
`build_graph` in `source.py` turns them into typed vertices + edges. Kinds:

    section    {title, level, page, path[], body, tag}           one per heading
    parameter  {name, description, range, section_path[]}        one per param-table row
    filtertype {name, description, section_path[]}               parsed from "Filter Types" body
    modsource  {name, description, section_path[]}               LFO "Shapes" rows
    xref       {from_path[], to_title}                           "See <Section>" cross-references
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import fitz  # PyMuPDF

# font size (pt) → heading level. These four sizes are used ONLY for headings in this
# document (body is 12, footer 8); see the module docstring / the calibration probe.
HEADING_SIZES = {22.5: 1, 18.0: 2, 15.0: 3, 13.5: 4}
BODY_SIZE = 12.0
FOOTER_SIZE = 8.0
# Page footer lines: the print URL and the "N/118" page counter.
_FOOTER_RE = re.compile(r"^\s*(?:https?://\S*manual-xt\S*|\d+\s*/\s*\d+)\s*$")
# A "Name - definition" line in the Filter Types section (space-dash-space separator).
# The name is short (≤6 words, no trailing period) so full sentences don't match; numbered
# sub-type lines ("1. Standard - …") are excluded by requiring a non-digit-list start.
_FILTERTYPE_RE = re.compile(r"^(?P<name>(?!\d+\.)[A-Za-z0-9][A-Za-z0-9 &/()+-]{1,34}?) - (?P<def>.+)$")
# Lines that END a filter type's main description (its sub-type list, credit, or reference
# aside) vs. superscript-ordinal noise ("st"/"nd"/"rd"/"th") to skip mid-paragraph.
_FT_STOP_RE = re.compile(r"^(?:Sub-types|Thanks|For more information|\d+\.\s)", re.I)
_FT_JUNK_RE = re.compile(r"^(?:st|nd|rd|th)$", re.I)
# "See <Section Title>" cross-reference (capture a Title-Cased phrase after "see").
_XREF_RE = re.compile(r"[Ss]ee (?:the )?(?P<t>[A-Z][A-Za-z0-9][A-Za-z0-9 &/'-]{2,38})")

# Sections whose ancestor path marks them as first-class synth entities (see `classify`).
_MODULE_TITLES = {"Oscillator Algorithms", "Filters", "Effects", "Modulators", "LFOs"}


def classify(path: list[str], level: int) -> str:
    """Vertex tag for a heading, from its ancestor path. The heading tree already encodes
    the taxonomy (under Technical Reference, each osc algorithm / effect is its own H3), so
    classification is just "which subtree am I in"."""
    anc, title = path[:-1], path[-1]
    if "Oscillator Algorithms" in anc and level == 3:
        return "OscillatorType"
    if "Technical Reference" in anc and "Effects" in anc and level == 3:
        return "Effect"
    if "Internal Modulators" in anc:
        return "ModulationSource"
    if title in _MODULE_TITLES and level in (2, 3):
        return "Module"
    return "Section"


def _line_size_text(line: dict) -> tuple[float, str]:
    spans = line.get("spans", [])
    text = "".join(s["text"] for s in spans).strip()
    size = round(max((s["size"] for s in spans), default=0.0), 1)
    return size, text


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _table_rows(page, table) -> list[list[str]]:
    """Clean cell text for a detected table: structure from find_tables, text from
    clip-extraction (which — unlike table.extract() — is not mangled by the Type3 font)."""
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        for c in row.cells:
            cells.append(_clean(page.get_text("text", clip=fitz.Rect(c))) if c else "")
        rows.append(cells)
    return rows


def _emit_table(section: dict | None, rows: list[list[str]],
                params: list[dict], modsources: list[dict]) -> None:
    """A detected table under `section` → parameter rows (name/description/range), or
    modulation-source rows when it's the LFO "Shapes" table. Empty-name rows (wrapped-cell
    artifacts) are skipped."""
    if section is None:
        return
    is_shapes = section["title"] == "Shapes"
    for cells in rows:
        name = cells[0].strip() if cells else ""
        if not name:
            continue
        desc = cells[1].strip() if len(cells) > 1 else ""
        rng = cells[2].strip() if len(cells) > 2 else ""
        if is_shapes:
            modsources.append({"kind": "modsource", "name": name,
                               "description": _clean(f"{desc} (deform: {rng})" if rng else desc),
                               "section_path": section["path"]})
        elif desc or rng:  # a param row with neither description nor range carries no info
            params.append({"kind": "parameter", "name": name, "description": desc,
                           "range": rng, "section_path": section["path"]})


def _parse_filtertypes(body_lines: list[str], section_path: list[str]) -> list[dict]:
    """Filter types are "Name - definition" lines in the Filter Types section body. Each
    type's main description continues across wrapped lines until its sub-type list / credit /
    reference aside begins — accumulate those so the node carries the full paragraph, not a
    mid-sentence fragment (thin text is what made these lose to parameter nodes in search)."""
    out: list[dict] = []
    cur: dict | None = None
    for raw in body_lines:
        ln = raw.strip()
        if not ln:
            continue
        m = _FILTERTYPE_RE.match(ln)
        # A real type name is a short label, not a sentence fragment.
        if m and len(m.group("name").split()) <= 6 and not m.group("name").endswith("."):
            cur = {"kind": "filtertype", "name": m.group("name").strip(),
                   "description": m.group("def").strip(), "section_path": section_path}
            out.append(cur)
        elif cur is not None:
            if _FT_JUNK_RE.match(ln):
                continue                       # superscript ordinal noise — skip, don't stop
            if _FT_STOP_RE.match(ln):
                cur = None                     # sub-types / credits begin — description done
            else:
                cur["description"] = _clean(cur["description"] + " " + ln)
    return out


def _find_xrefs(sections: list[dict]) -> list[dict]:
    """`See <Title>` mentions in a section body → a cross-reference to that section."""
    by_title: dict[str, list[str]] = {}
    for s in sections:
        by_title.setdefault(s["title"].lower(), s["path"])  # first occurrence wins
    out: list[dict] = []
    seen: set[tuple] = set()
    for s in sections:
        for m in _XREF_RE.finditer(s["body"]):
            phrase = re.sub(r"\s+section$", "", m.group("t").strip(), flags=re.I).strip().lower()
            target = by_title.get(phrase)
            if target and target != s["path"]:
                key = (tuple(s["path"]), tuple(target))
                if key not in seen:
                    seen.add(key)
                    out.append({"kind": "xref", "from_path": s["path"], "to_path": target})
    return out


# ---------------------------------------------------------------------------
# FIGURES — the manual's screenshots (embedded raster) and block diagrams /
# illustrations (drawn as vectors). Both are found per page and attached to the
# section open at their y-position (via the same items walk as tables). Only run
# when `extract(image_dir=...)` is given.
# ---------------------------------------------------------------------------
_MIN_RASTER_W, _MIN_RASTER_H = 80.0, 40.0  # on-page points — drop icons/decorations


def _raster_figures(page) -> list[tuple[float, tuple]]:
    """(y0, ('raster', xref, bbox)) for each substantial embedded image."""
    out = []
    for im in page.get_image_info(xrefs=True):
        b, xref = im.get("bbox"), im.get("xref", 0)
        if not b or not xref:
            continue
        r = fitz.Rect(b)
        if r.width >= _MIN_RASTER_W and r.height >= _MIN_RASTER_H:
            out.append((r.y0, ("raster", xref, r)))
    return out


def _overlap_frac(a: fitz.Rect, b: fitz.Rect) -> float:
    """Fraction of `a` covered by `b`."""
    ix = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    iy = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    area = a.width * a.height
    return (ix * iy / area) if area else 0.0


def _vector_figures(page, raster_rects: list[fitz.Rect]) -> list[tuple[float, tuple]]:
    """(y0, ('vector', clip)) for dense drawing-cluster bands — the block diagrams /
    illustrations that are drawn, not embedded. Noise (hairlines, full-width rules,
    page backgrounds) is dropped first; rects are grouped into y-bands; a band with
    enough drawings and a figure-sized height becomes one rendered figure. Bands
    already covered by a raster image are skipped (no double-capture)."""
    pw, ph = page.rect.width, page.rect.height
    rects = []
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"])
        if r.width < 6 or r.height < 6:
            continue
        if r.width > 0.85 * pw and r.height < 4:      # full-width hairline rule
            continue
        if r.width > 0.9 * pw and r.height > 0.9 * ph:  # page-background rect
            continue
        rects.append(r)
    if not rects:
        return []
    rects.sort(key=lambda r: (r.y0 + r.y1) / 2)
    bands: list[list[fitz.Rect]] = [[rects[0]]]
    bot = rects[0].y1
    for r in rects[1:]:
        if r.y0 <= bot + 18:              # same vertical band
            bands[-1].append(r)
            bot = max(bot, r.y1)
        else:
            bands.append([r])
            bot = r.y1
    out = []
    for band in bands:
        if len(band) < 12:                # need a dense cluster (a real figure)
            continue
        clip = fitz.Rect(min(r.x0 for r in band), min(r.y0 for r in band),
                         max(r.x1 for r in band), max(r.y1 for r in band))
        if clip.height < 25 or clip.height > 0.8 * ph:
            continue
        if any(_overlap_frac(clip, rr) > 0.5 for rr in raster_rects):
            continue
        out.append((clip.y0, ("vector", clip)))
    return out


def _emit_figure(section: dict, page, doc, payload: tuple, image_dir: str,
                 seen: set, cids: dict) -> None:
    """Extract/render one figure to `image_dir` as `<sha1[:12]>.png` and append a ref
    to the section's `images` list. Raster → original embedded bytes; vector → the
    band region rendered at 150 dpi. When the figure has been pinned to IPFS (its file
    is in `cids`, from `pin.py`), the ref also carries its `cid` so it travels on-chain."""
    kind = payload[0]
    try:
        if kind == "raster":
            d = doc.extract_image(payload[1])
            data, ext = d["image"], d.get("ext", "png")
            w, h = d.get("width"), d.get("height")
        else:
            pix = page.get_pixmap(clip=payload[1], dpi=150)
            data, ext, w, h = pix.tobytes("png"), "png", pix.width, pix.height
    except Exception:  # noqa: BLE001 — a bad figure must never abort extraction
        return
    fname = f"{hashlib.sha1(data).hexdigest()[:12]}.{ext}"
    if fname not in seen:
        seen.add(fname)
        with open(os.path.join(image_dir, fname), "wb") as f:
            f.write(data)
    if not any(im["file"] == fname for im in section["images"]):
        ref = {"file": fname, "w": w, "h": h, "kind": kind}
        if fname in cids:
            ref["cid"] = cids[fname]
        section["images"].append(ref)


def extract(pdf_path: str, image_dir: str | None = None) -> list[dict]:
    """Parse the Surge XT manual PDF into raw records (see module docstring). When
    `image_dir` is given, also extract each section's figures (screenshots + diagrams)
    into that directory and record `images` refs on the section records."""
    doc = fitz.open(pdf_path)
    seen_figs: set[str] = set()
    img_cids: dict[str, str] = {}   # figure filename → pinned IPFS CID (from pin.py), if any
    if image_dir:
        os.makedirs(image_dir, exist_ok=True)
        cids_path = os.path.join(image_dir, "image-cids.json")
        if os.path.exists(cids_path):
            try:
                img_cids = json.load(open(cids_path))
            except (json.JSONDecodeError, OSError):
                img_cids = {}
    sections: list[dict] = []
    params: list[dict] = []
    modsources: list[dict] = []
    body_lines_by_section: dict[int, list[str]] = {}   # id(section) → its raw body lines
    stack: list[tuple[int, dict]] = []                 # (level, section) open ancestors

    for pno, page in enumerate(doc, 1):
        tables = page.find_tables().tables
        table_rects = [fitz.Rect(t.bbox) for t in tables]

        # One ordered stream of items per page: heading / body lines (clean, from dict) and
        # tables, sorted top-to-bottom so a table attaches to whatever section is open above it.
        items: list[tuple[float, str, object]] = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                size, text = _line_size_text(line)
                if not text or size == FOOTER_SIZE or _FOOTER_RE.match(text):
                    continue
                y0 = line["bbox"][1]
                if any(r.y0 - 1 <= y0 <= r.y1 + 1 for r in table_rects):
                    continue  # inside a table → handled as a table row, not body
                lvl = HEADING_SIZES.get(size, 0)
                items.append((y0, "head" if lvl else "body", (lvl, text)))
        for t, r in zip(tables, table_rects):
            items.append((r.y0, "table", t))
        if image_dir:
            rfigs = _raster_figures(page)
            for y0, fp in rfigs:
                items.append((y0, "figure", fp))
            for y0, fp in _vector_figures(page, [fp[2] for _y, fp in rfigs]):
                items.append((y0, "figure", fp))
        items.sort(key=lambda it: it[0])

        for _y, kind, payload in items:
            if kind == "head":
                lvl, title = payload
                while stack and stack[-1][0] >= lvl:
                    stack.pop()
                path = [s[1]["title"] for s in stack] + [title]
                sec = {"kind": "section", "title": title, "level": lvl, "page": pno,
                       "path": path, "body": "", "tag": classify(path, lvl), "images": []}
                sections.append(sec)
                body_lines_by_section[id(sec)] = []
                stack.append((lvl, sec))
            elif kind == "body":
                if stack:
                    body_lines_by_section[id(stack[-1][1])].append(payload[1])
            elif kind == "figure":
                if stack and image_dir:
                    _emit_figure(stack[-1][1], page, doc, payload, image_dir, seen_figs, img_cids)
            else:  # table
                _emit_table(stack[-1][1] if stack else None,
                            _table_rows(page, payload), params, modsources)

    # Finalize bodies; parse the Filter Types list from its section's raw body lines.
    filtertypes: list[dict] = []
    for sec in sections:
        lines = body_lines_by_section[id(sec)]
        sec["body"] = _clean(" ".join(lines))
        if sec["title"] == "Filter Types":
            filtertypes.extend(_parse_filtertypes(lines, sec["path"]))

    xrefs = _find_xrefs(sections)
    return [*sections, *params, *filtertypes, *modsources, *xrefs]


if __name__ == "__main__":  # calibration: `python -m quickbeam_surgext.extract <pdf>`
    import sys
    from collections import Counter
    recs = extract(sys.argv[1] if len(sys.argv) > 1 else "Surge-XT-Manual.pdf")
    kinds = Counter(r["kind"] for r in recs)
    tags = Counter(r["tag"] for r in recs if r["kind"] == "section")
    print("records by kind:", dict(kinds))
    print("section tags:   ", dict(tags))
    for kind in ("parameter", "filtertype", "modsource"):
        ex = next((r for r in recs if r["kind"] == kind), None)
        print(f"sample {kind}:", ex)
    print("effects:", [r["title"] for r in recs
                       if r["kind"] == "section" and r["tag"] == "Effect"])
    print("osc types:", [r["title"] for r in recs
                         if r["kind"] == "section" and r["tag"] == "OscillatorType"])
    print("filter types:", [r["name"] for r in recs if r["kind"] == "filtertype"])
    print("mod sources:", [r["name"] for r in recs if r["kind"] == "modsource"])
