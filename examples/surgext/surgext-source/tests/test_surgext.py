"""
Tests for the surgext source.

  test_build_graph_*   — PURE: hand-built records → assert on the graph (no PDF).
  test_extract_pdf_*   — smoke: run the extractor against the real manual PDF and assert the
                         known taxonomy is recovered. Skipped if the PDF isn't present.
"""
import json
import os

import pytest

from quickbeam_surgext.source import build_graph
from quickbeam_surgext import extract as ex

PDF = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Surge-XT-Manual.pdf")


def _has_edge(edges, rel, frm, to):
    return any(e["rel"] == rel and e["from"] == frm and e["to"] == to for e in edges)


# --------------------------------------------------------------------------- pure
HAND_RECORDS = [
    {"kind": "section", "title": "Technical Reference", "level": 1, "page": 77,
     "path": ["Technical Reference"], "body": "", "tag": "Section"},
    {"kind": "section", "title": "Effects", "level": 2, "page": 98,
     "path": ["Technical Reference", "Effects"], "body": "", "tag": "Module"},
    {"kind": "section", "title": "Distortion", "level": 3, "page": 100,
     "path": ["Technical Reference", "Effects", "Distortion"],
     "body": "Distortion algorithm. Provides EQ options.", "tag": "Effect"},
    {"kind": "parameter", "name": "Drive", "description": "Drive of the clipping stage",
     "range": "-24 .. 24 dB", "section_path": ["Technical Reference", "Effects", "Distortion"]},
    {"kind": "section", "title": "Filters", "level": 2, "page": 92,
     "path": ["Technical Reference", "Filters"], "body": "", "tag": "Module"},
    {"kind": "section", "title": "Filter Types", "level": 3, "page": 93,
     "path": ["Technical Reference", "Filters", "Filter Types"], "body": "", "tag": "Section"},
    {"kind": "filtertype", "name": "Vintage Ladder", "description": "4-pole ladder filter.",
     "section_path": ["Technical Reference", "Filters", "Filter Types"]},
    {"kind": "section", "title": "Shapes", "level": 4, "page": 39,
     "path": ["Modulation", "Modulators", "LFOs", "Shapes"], "body": "", "tag": "Section"},
    {"kind": "modsource", "name": "Sine", "description": "Sine wave LFO",
     "section_path": ["Modulation", "Modulators", "LFOs", "Shapes"]},
    {"kind": "xref", "from_path": ["Technical Reference", "Effects", "Distortion"],
     "to_path": ["Technical Reference", "Filters", "Filter Types"]},
]


def test_build_graph_nodes_and_edges():
    nodes, edges = build_graph(HAND_RECORDS)

    # Buckets keyed by entityType; every node carries its entityType.
    assert set(nodes) == {"Section", "Module", "Effect", "FilterType", "ModulationSource", "Parameter"}
    for entity, node_list in nodes.items():
        for n in node_list:
            assert n["fields"]["entityType"] == entity

    dist = "surgext:technical-reference/effects/distortion"
    effect = nodes["Effect"][0]
    assert effect["name"] == dist
    assert "Distortion algorithm" in effect["fields"]["text"]

    drive = next(n for n in nodes["Parameter"] if n["name"].endswith("/param/drive"))
    assert drive["fields"]["range"] == "-24 .. 24 dB"
    assert "Range:" in drive["fields"]["text"]

    ft = "surgext:filtertype:vintage-ladder"
    ms = "surgext:modsource:sine"
    filt = "surgext:technical-reference/filters/filter-types"

    assert _has_edge(edges, "hasSubsection", "surgext:technical-reference/effects", dist)
    assert _has_edge(edges, "hasParameter", dist, drive["name"])
    assert _has_edge(edges, "hasType", filt, ft)
    assert _has_edge(edges, "providesModulation", "surgext:modulation/modulators/lfos/shapes", ms)
    assert _has_edge(edges, "seeAlso", dist, filt)


def test_build_graph_is_deterministic():
    # Reproducibility is load-bearing: identical records must yield identical output so
    # Fangorn's content-addressing shares unchanged blocks across commits.
    assert build_graph(HAND_RECORDS) == build_graph(HAND_RECORDS)


def test_build_graph_skips_edges_to_missing_nodes():
    # A parameter whose section wasn't emitted must not crash or dangle an edge.
    recs = [{"kind": "parameter", "name": "X", "description": "d", "range": "",
             "section_path": ["Nope"]}]
    nodes, edges = build_graph(recs)
    assert edges == [] and "Parameter" not in nodes  # no parent → param dropped


# --------------------------------------------------------------------------- real PDF
requires_pdf = pytest.mark.skipif(not os.path.exists(PDF), reason="Surge-XT-Manual.pdf not present")


@requires_pdf
def test_extract_recovers_taxonomy():
    recs = ex.extract(PDF)
    by_kind = lambda k: [r for r in recs if r["kind"] == k]

    sections = by_kind("section")
    assert len(sections) > 150

    effects = {r["title"] for r in sections if r["tag"] == "Effect"}
    for name in ("Distortion", "Reverb 1", "Reverb 2", "Chorus", "Vocoder", "Delay"):
        assert name in effects
    assert len(effects) >= 25

    osc = {r["title"] for r in sections if r["tag"] == "OscillatorType"}
    assert {"Classic", "Wavetable", "FM2", "FM3", "String", "Twist"} <= osc

    ftypes = {r["name"] for r in by_kind("filtertype")}
    assert "Vintage Ladder" in ftypes and "Legacy Ladder" in ftypes

    shapes = {r["name"] for r in by_kind("modsource")}
    assert {"Sine", "Triangle", "Sawtooth", "S&H"} <= shapes

    # A real parameter with a numeric range came through cleanly (no Type3 mangling).
    params = by_kind("parameter")
    assert len(params) > 200
    ranged = [p for p in params if ".." in p["range"]]
    assert ranged and all("distortoi" not in p["description"].lower() for p in params)


@requires_pdf
def test_build_graph_over_real_pdf():
    nodes, edges = build_graph(ex.extract(PDF))
    assert nodes["Effect"] and nodes["Parameter"] and nodes["OscillatorType"]
    rels = {e["rel"] for e in edges}
    assert {"hasSubsection", "hasParameter", "hasType", "providesModulation"} <= rels


# --------------------------------------------------------------- patch fusion (pure)
# HAND_RECORDS already yields Effect "Distortion" + FilterType "Vintage Ladder"; add an
# OscillatorType "Classic" so a patch can link all three axes.
_OSC_RECORDS = [
    {"kind": "section", "title": "Oscillator Algorithms", "level": 2, "page": 79,
     "path": ["Technical Reference", "Oscillator Algorithms"], "body": "", "tag": "Module"},
    {"kind": "section", "title": "Classic", "level": 3, "page": 79,
     "path": ["Technical Reference", "Oscillator Algorithms", "Classic"],
     "body": "Classic oscillator.", "tag": "OscillatorType"},
]
_PATCH = {"kind": "patch", "path": "/x/patches_factory/Basses/Behemoth.fxp", "name": "Behemoth",
          "category": "Basses", "author": "Claes", "tags": ["fat"],
          "filters": [("LP", "Vintage Ladder")], "effects": ["Distortion"],
          "oscillators": ["Classic"], "comment": "A fat analog bass."}


def test_patch_fusion_nodes_edges_and_backfill():
    nodes, edges = build_graph(HAND_RECORDS + _OSC_RECORDS + [_PATCH])

    patch = nodes["Patch"][0]
    assert patch["name"] == "surgext:patch:basses-behemoth"
    assert patch["fields"]["entityType"] == "Patch"
    assert "fat analog bass" in patch["fields"]["text"].lower()

    dist = "surgext:technical-reference/effects/distortion"
    vl = "surgext:filtertype:vintage-ladder"
    classic = "surgext:technical-reference/oscillator-algorithms/classic"
    assert _has_edge(edges, "usesFilter", patch["name"], vl)
    assert _has_edge(edges, "usesEffect", patch["name"], dist)
    assert _has_edge(edges, "usesOscillator", patch["name"], classic)

    # The perceptual vocabulary was back-filled into the filter node's embedded text.
    vl_text = next(n for n in nodes["FilterType"] if n["name"] == vl)["fields"]["text"]
    assert "Heard in patches: Basses" in vl_text and "lowpass" in vl_text


def test_patch_fusion_is_deterministic():
    assert build_graph(HAND_RECORDS + _OSC_RECORDS + [_PATCH]) == \
           build_graph(HAND_RECORDS + _OSC_RECORDS + [_PATCH])


# --------------------------------------------------------------- patch reader (real DB)
from quickbeam_surgext.patches import DEFAULT_DB, FILTER_ALIAS, read_patches  # noqa: E402

requires_db = pytest.mark.skipif(not os.path.exists(DEFAULT_DB), reason="Surge patch DB not present")


@requires_db
def test_read_patches_audibility_and_coverage():
    recs = read_patches()
    assert len(recs) > 2000

    beh = next(r for r in recs if r["name"] == "Behemoth")
    # Only the audible oscillator is linked — Behemoth's osc2/osc3 are muted.
    assert beh["oscillators"] == ["Classic"]
    assert ("LP", "12 dB") in beh["filters"]

    # Every filter name the DB emits must be in the alias table (guards Surge-version drift).
    filter_names = {n for r in recs for _, n in r["filters"]}
    assert filter_names <= set(FILTER_ALIAS), f"unmapped: {filter_names - set(FILTER_ALIAS)}"


# --------------------------------------------------------------- figures (images)
def test_build_graph_carries_section_images():
    recs = [
        {"kind": "section", "title": "Distortion", "level": 3, "page": 100,
         "path": ["Technical Reference", "Effects", "Distortion"],
         "body": "Distortion algorithm.", "tag": "Effect",
         "images": [{"file": "abc123def456.png", "w": 1040, "h": 840, "kind": "vector"}]},
    ]
    nodes, _ = build_graph(recs)
    eff = nodes["Effect"][0]
    assert eff["fields"]["images"] == recs[0]["images"]
    assert "abc123" not in eff["fields"]["text"]  # figures never leak into the embedded text


@requires_pdf
def test_extract_images(tmp_path):
    recs = ex.extract(PDF, image_dir=str(tmp_path))
    secs = [r for r in recs if r["kind"] == "section"]
    withimg = [s for s in secs if s.get("images")]
    assert len(withimg) >= 40  # screenshots + diagrams across the manual
    kinds = {im["kind"] for s in secs for im in s["images"]}
    assert {"raster", "vector"} <= kinds  # both figure types captured
    # Distortion carries its (vector) block diagram.
    dist = next(s for s in secs if s["title"] == "Distortion")
    assert any(im["kind"] == "vector" for im in dist["images"])
    # Every referenced file was actually written.
    files = set(os.listdir(tmp_path))
    assert files and all(im["file"] in files for s in secs for im in s["images"])


# --------------------------------------------------------------- pinning (parallel)
from quickbeam_surgext import pin as pinmod  # noqa: E402


def _make_pngs(tmp_path, n):
    for i in range(n):
        (tmp_path / f"img{i:03d}.png").write_bytes(b"\x89PNG" + bytes([i]))
    return sorted(p.name for p in tmp_path.glob("*.png"))


def test_pin_images_parallel_skips_and_resumes(tmp_path, monkeypatch):
    names = _make_pngs(tmp_path, 12)
    calls = []
    monkeypatch.setattr(pinmod, "_pin_one",
                        lambda path, name, jwt, **kw: (calls.append(name), f"cid-{name}")[1])

    cids = pinmod.pin_images(str(tmp_path), "jwt", verbose=False, concurrency=4)
    assert cids == {n: f"cid-{n}" for n in names}
    assert sorted(calls) == names                      # every file pinned exactly once
    assert json.loads((tmp_path / "image-cids.json").read_text()) == cids

    calls.clear()                                      # re-run is a no-op (skip-if-present)
    assert pinmod.pin_images(str(tmp_path), "jwt", verbose=False) == cids
    assert calls == []


def test_pin_images_persists_partial_on_failure(tmp_path, monkeypatch):
    _make_pngs(tmp_path, 6)

    def flaky(path, name, jwt, **kw):
        if name == "img003.png":
            raise RuntimeError("pinata exploded")
        return f"cid-{name}"

    monkeypatch.setattr(pinmod, "_pin_one", flaky)
    with pytest.raises(RuntimeError):
        pinmod.pin_images(str(tmp_path), "jwt", verbose=False, concurrency=1)

    # The CIDs earned before the failure survive, so a re-run resumes instead of restarting.
    saved = json.loads((tmp_path / "image-cids.json").read_text())
    assert saved and "img003.png" not in saved
    assert all(v == f"cid-{k}" for k, v in saved.items())
