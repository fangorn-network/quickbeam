"""
`SurgeXTSource` — a pluggable quickbeam `Source` that turns the Surge XT user-manual PDF
into a rich Fangorn knowledge graph.

Follows the same pattern as the `example-robinhood-source`: the source supplies only
read + shape (`build_graph`) + cursor; the harness owns the CLI, staged-volume emission,
`--dry-run`, and `--publish` to fangorn. Installing this package registers a
`quickbeam.sources` entry point, so `quickbeam data surgext` appears automatically and the
class is also usable via `quickbeam.Publisher`.

The manual is a STATIC document, so this is a batch source: `read` parses the PDF once
(cursor ignored) and `next_cursor` never advances. Re-running produces byte-identical
records, so a re-publish uploads only true deltas (Fangorn structural sharing).

Graph shape (`build_graph`, PURE — hand-build records → assert on nodes/edges):
    vertices  {entityType: [{"name": <stable id>, "fields": {...}}]}
    edges     [{"rel", "from", "to", "fromType", "toType"}]
Entity types: Section, Module, Effect, OscillatorType, FilterType, ModulationSource, Parameter.
Relations:    hasSubsection, hasParameter, hasType, providesModulation, seeAlso.
`fields.text` is the embedded document; `fields.title`/`fields.category` drive the role map.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict

from .extract import extract
from .patches import DEFAULT_DB, FILTER_ALIAS, FX_ALIAS, MODE_WORD, read_patches

__all__ = ["SurgeXTSource", "build_graph", "run"]

# entity_type → staged volume stem (the volume_<n>_<stem>.json suffix).
STEMS = {
    "Section": "sections", "Module": "modules", "Effect": "effects",
    "OscillatorType": "osctypes", "FilterType": "filtertypes",
    "ModulationSource": "modsources", "Parameter": "parameters", "Patch": "patches",
}

SURGEXT_ROLE_MAP = {"title": "title", "subtitle": "", "tags": ["category"], "text": ["text"]}

SURGEXT_PRESENTATION = {
    "accent": "#ff9500",  # Surge orange
    "icons": {
        "Section": "article", "Module": "widgets", "Effect": "graphic_eq",
        "OscillatorType": "waves", "FilterType": "filter_alt",
        "ModulationSource": "sync_alt", "Parameter": "tune", "Patch": "piano",
    },
}


# ---------------------------------------------------------------------------
# IDS + TEXT — pure helpers (deterministic → content-addressing stays stable).
# ---------------------------------------------------------------------------
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _sid(path: list[str]) -> str:
    """Stable id for a heading node: its breadcrumb slug. Stable across PDF regenerations
    (content-based, not page-based), and unique because same-named headings differ in path."""
    return "surgext:" + "/".join(_slug(p) for p in path)


def _crumb(path: list[str]) -> str:
    return " › ".join(path)


def _patch_id(path: str) -> str:
    """Stable patch id from its file path (not the DB rowid, which isn't content-stable)."""
    rel = os.path.basename(path)
    for marker in ("patches_factory/", "patches_3rdparty/", "Surge XT/Patches/"):
        if marker in path:
            rel = path.split(marker, 1)[1]
            break
    return "surgext:patch:" + _slug(rel[:-4] if rel.endswith(".fxp") else rel)


# ---------------------------------------------------------------------------
# GRAPH — raw records → typed vertices + edges (the harness data contract).
# ---------------------------------------------------------------------------
def build_graph(records: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Turn `extract`'s raw records into ({entityType: [{name, fields}]}, [edges]). PURE."""
    nodes: dict[str, dict[str, dict]] = {}          # entityType → {id: node} (dedup, order-preserving)
    edges: list[dict] = []
    sections = {tuple(r["path"]): r for r in records if r["kind"] == "section"}

    def add_node(entity: str, node_id: str, fields: dict) -> None:
        fields["entityType"] = entity
        nodes.setdefault(entity, {})[node_id] = {"name": node_id, "fields": fields}

    def add_edge(rel: str, src: tuple[str, str] | None, dst: tuple[str, str] | None) -> None:
        if src and dst:
            edges.append({"rel": rel, "from": src[0], "to": dst[0],
                          "fromType": src[1], "toType": dst[1]})

    def endpoint(path: list[str]) -> tuple[str, str] | None:
        """(id, entityType) for a heading node by path, or None if unknown."""
        rec = sections.get(tuple(path))
        return (_sid(path), rec["tag"]) if rec else None

    # 1) Heading nodes (Section/Module/Effect/OscillatorType/ModulationSource) + the tree.
    for rec in records:
        if rec["kind"] != "section":
            continue
        path, tag = rec["path"], rec["tag"]
        body, crumb = rec["body"], _crumb(path)
        text = f"{crumb} — {body}" if body else crumb
        fields = {
            "title": rec["title"], "category": path[0], "breadcrumb": crumb,
            "page": rec["page"], "text": text,
        }
        if rec.get("images"):  # figure refs (kept out of `text` — embeddings unchanged)
            fields["images"] = rec["images"]
        add_node(tag, _sid(path), fields)
        if len(path) > 1:  # link from parent heading (always an existing node — it's an ancestor)
            add_edge("hasSubsection", endpoint(path[:-1]), (_sid(path), tag))

    # 2) Parameters (from effect/module tables) → nodes + hasParameter from their section.
    for rec in records:
        if rec["kind"] != "parameter":
            continue
        parent = endpoint(rec["section_path"])
        if not parent:
            continue
        name, desc, rng = rec["name"], rec["description"], rec["range"]
        pid = f"{parent[0]}/param/{_slug(name)}"
        text = f"{name} — {desc}".rstrip(". ") + "."
        if rng:
            text += f" Range: {rng}."
        text += f" (Parameter of {rec['section_path'][-1]}.)"
        fields = {"title": name, "category": rec["section_path"][-1], "text": text,
                  "parent": _crumb(rec["section_path"])}
        if rng:
            fields["range"] = rng
        add_node("Parameter", pid, fields)
        add_edge("hasParameter", parent, (pid, "Parameter"))

    # 3) Filter types (parsed from the Filter Types section) → nodes + hasType.
    for rec in records:
        if rec["kind"] != "filtertype":
            continue
        fid = "surgext:filtertype:" + _slug(rec["name"])
        # Keyword-anchored so a "…filter" query ranks the type node, not just params that
        # happen to mention "filter" (the search soft spot these type nodes had).
        add_node("FilterType", fid, {
            "title": rec["name"], "category": "Filter Type",
            "text": f"Filter type: {rec['name']}. {rec['description']}"})
        add_edge("hasType", endpoint(rec["section_path"]), (fid, "FilterType"))

    # 4) Modulation sources (LFO shapes) → nodes + providesModulation.
    for rec in records:
        if rec["kind"] != "modsource":
            continue
        mid = "surgext:modsource:" + _slug(rec["name"])
        add_node("ModulationSource", mid, {
            "title": rec["name"], "category": "Modulation Source",
            "text": f"LFO modulation shape: {rec['name']}. {rec['description']}"})
        add_edge("providesModulation", endpoint(rec["section_path"]), (mid, "ModulationSource"))

    # 5) Cross-references ("See <Section>") → seeAlso.
    for rec in records:
        if rec["kind"] == "xref":
            add_edge("seeAlso", endpoint(rec["from_path"]), endpoint(rec["to_path"]))

    # 6) FUSION — the Surge patch library (second source): Patch nodes + usesFilter/usesEffect/
    #    usesOscillator edges to the manual entities, and a vocabulary back-fill into those
    #    entities' text (the perceptual words — categories, lowpass/… — the manual never uses).
    patch_recs = [r for r in records if r["kind"] == "patch"]
    if patch_recs:
        title_idx = {ent: {n["fields"]["title"]: nid for nid, n in nodes.get(ent, {}).items()}
                     for ent in ("FilterType", "Effect", "OscillatorType")}
        manual_by_id = {nid: n for ent in title_idx for nid, n in nodes.get(ent, {}).items()}
        backfill: dict[str, dict] = defaultdict(lambda: {"cats": [], "modes": set(), "examples": []})
        unmapped: set[str] = set()

        def _link(target_id: str, category: str, name: str, mode: str = "") -> None:
            bf = backfill[target_id]
            bf["cats"].append(category)
            if mode in MODE_WORD:
                bf["modes"].add(MODE_WORD[mode])
            if name not in bf["examples"] and len(bf["examples"]) < 8:
                bf["examples"].append(name)

        for r in patch_recs:
            pid = _patch_id(r["path"])
            fnames = [f"{MODE_WORD.get(m, '')} {n}".strip() for m, n in r["filters"]]
            seg = [f"{r['name']} — a {r['category']} patch"
                   + (f" by {r['author']}" if r["author"] else "") + "."]
            if r["comment"]:
                seg.append(r["comment"])
            if fnames:
                seg.append(f"Filters: {', '.join(fnames)}.")
            if r["oscillators"]:
                seg.append(f"Oscillators: {', '.join(r['oscillators'])}.")
            if r["effects"]:
                seg.append(f"Effects: {', '.join(r['effects'])}.")
            if r["tags"]:
                seg.append(f"Tags: {', '.join(r['tags'])}.")
            add_node("Patch", pid, {"title": r["name"], "category": r["category"],
                                    "author": r["author"], "text": re.sub(r"\s+", " ", " ".join(seg)).strip()})

            for mode, fname in r["filters"]:
                title = FILTER_ALIAS.get(fname)
                fid = title_idx["FilterType"].get(title) if title else None
                if fid:
                    _link(fid, r["category"], r["name"], mode)
                    add_edge("usesFilter", (pid, "Patch"), (fid, "FilterType"))
                else:
                    unmapped.add(f"FILTER:{fname}")
            for fx in r["effects"]:
                eid = title_idx["Effect"].get(FX_ALIAS.get(fx, fx))
                if eid:
                    _link(eid, r["category"], r["name"])
                    add_edge("usesEffect", (pid, "Patch"), (eid, "Effect"))
                else:
                    unmapped.add(f"FX:{fx}")
            for osc in r["oscillators"]:
                oid = title_idx["OscillatorType"].get(osc)
                if oid:
                    _link(oid, r["category"], r["name"])
                    add_edge("usesOscillator", (pid, "Patch"), (oid, "OscillatorType"))
                else:
                    unmapped.add(f"OSC:{osc}")

        # Back-fill the perceptual vocabulary into each manual entity's embedded text.
        for eid, bf in backfill.items():
            top = ", ".join(c for c, _ in Counter(bf["cats"]).most_common(6))
            extra = f" Heard in patches: {top}"
            if bf["modes"]:
                extra += f"; used as {', '.join(sorted(bf['modes']))}"
            if bf["examples"]:
                extra += f"; e.g. {', '.join(bf['examples'][:5])}"
            manual_by_id[eid]["fields"]["text"] += extra + "."

        if unmapped:
            print(f"[surgext] WARNING: {len(unmapped)} unmapped patch feature value(s): "
                  f"{', '.join(sorted(unmapped))}", file=sys.stderr)

    return {entity: list(by_id.values()) for entity, by_id in nodes.items()}, edges


# ---------------------------------------------------------------------------
# THE SOURCE — read + shape + cursor. Everything else is the harness's job.
# ---------------------------------------------------------------------------
class SurgeXTSource:
    """A batch `Source` over the Surge XT manual PDF. Structurally satisfies
    `quickbeam.ingest.scrapers.Source` (no import of quickbeam needed to match it)."""

    name = "surgext"
    stems = STEMS
    snapshot_stems = set(STEMS.values())  # static doc → every stem is rewritten wholesale
    role_map = SURGEXT_ROLE_MAP
    presentation = SURGEXT_PRESENTATION
    default_volume = 1

    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--pdf-path", default="./Surge-XT-Manual.pdf",
                       help="Path to the Surge XT manual PDF (default ./Surge-XT-Manual.pdf).")
        p.add_argument("--patch-db", default=DEFAULT_DB,
                       help="Surge patch index DB to fuse in as a second source (default "
                            f"{DEFAULT_DB!r}). Empty/absent → manual only.")
        p.add_argument("--patch-scope", default="all", choices=["all", "factory"],
                       help="Which patches to fuse: 'all' (factory + 3rd-party) or 'factory'.")
        p.add_argument("--image-dir", default=None,
                       help="Extract the manual's figures (screenshots + block diagrams) into this "
                            "directory and attach them to their sections. Omit → text only.")

    def read(self, cursor: int, args: argparse.Namespace) -> list[dict]:
        """Parse the PDF and fuse the patch library. `cursor` is ignored — static documents."""
        records = extract(args.pdf_path, image_dir=getattr(args, "image_dir", None))
        db = getattr(args, "patch_db", "")
        if db and os.path.exists(db):
            patches = read_patches(db, getattr(args, "patch_scope", "all"))
            print(f"[surgext] fusing {len(patches)} patch(es) from {db}")
            records += patches
        elif db:
            print(f"[surgext] patch DB not found ({db}); manual only.", file=sys.stderr)
        return records

    def build_graph(self, records: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
        return build_graph(records)

    def next_cursor(self, records: list[dict], prev: int) -> int:
        """Static document — nothing advances the cursor."""
        return prev


def run() -> None:
    """Console-script / entry-point target: hand the Source to the harness."""
    from quickbeam.ingest.scrapers.harness import run_source
    run_source(SurgeXTSource())


if __name__ == "__main__":
    run()
