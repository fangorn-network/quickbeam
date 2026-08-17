"""
The Surge patch library — the SECOND data source fused into the surgext graph.

Why: the manual describes filters/oscillators/effects *technically*, but people search
*perceptually* ("warm analog bass"). The factory + 3rd-party patches carry that missing
vocabulary — which filters/oscillators/effects each patch uses, its sound category, its
author's comment. `read_patches` reads them; `source.build_graph` turns them into Patch
nodes + edges to the manual's entity nodes and back-fills that vocabulary into those nodes.

Two sources, joined per patch:
  • SurgePatches.db  — Surge's own patch index. Already decodes DSP to STRINGS, so no enum
    work for filters/effects: `PatchFeature.feature_svalue` gives FILTER="LP Vintage Ladder"
    (mode + type), FX="Reverb 1", plus AUTHOR / TAG. `Patches` gives path/name/category.
  • the .fxp file    — for the two things the DB lacks: audible OSCILLATOR types and the
    patch COMMENT. The .fxp embeds clean patch XML; we slice `<?xml…</patch>` out of the
    bytes (no VST-chunk parsing) and read the params by regex.

Oscillator type is an integer enum in the .fxp; `_OSC_NAMES` is the authoritative order
extracted from the installed Surge 1.3.4 binary's `osc_type_names`. An oscillator counts
only if AUDIBLE (its scene is active and its mixer channel isn't muted) — otherwise every
patch's muted default osc3 (Classic) would link to everything.
"""
from __future__ import annotations

import html
import os
import re
import sqlite3
from collections import defaultdict

__all__ = ["read_patches", "DEFAULT_DB",
           "FILTER_ALIAS", "FX_ALIAS", "OSC_ALIAS", "MODE_WORD"]

DEFAULT_DB = os.path.expanduser("~/Documents/Surge XT/SurgePatches.db")

# Oscillator type enum → name (index == the .fxp integer value); from Surge 1.3.4's binary.
_OSC_NAMES = ["Classic", "Sine", "Wavetable", "S&H Noise", "Audio In", "FM3", "FM2",
              "Window", "Modern", "String", "Twist", "Alias"]

# Filter mode prefix (from the DB's "LP Vintage Ladder") → the perceptual word it stands for
# (this word is exactly what's missing from the manual — "LP" == "lowpass").
_MODES = {"LP", "HP", "BP", "N", "MULTI", "FX"}
MODE_WORD = {"LP": "lowpass", "HP": "highpass", "BP": "bandpass",
             "N": "notch", "MULTI": "multimode", "FX": "effect filter"}

# db/enum name → the manual node TITLE (source.build_graph resolves title → node id). Only the
# non-identity cases are listed; anything absent maps to itself.
FILTER_ALIAS = {
    "12 dB": "12 dB", "24 dB": "24 dB", "Legacy Ladder": "Legacy Ladder",
    "Vintage Ladder": "Vintage Ladder", "K35": "K35", "Diode Ladder": "Diode Ladder",
    "OB-Xd 12 dB": "OB-Xd 12dB", "OB-Xd 24 dB": "OB-Xd 24 dB",
    "Cutoff Warp": "Cutoff Warp", "Cutoff Warp AP": "Cutoff Warp",
    "Res Warp": "Resonance Warp", "Res Warp AP": "Resonance Warp",
    "Tri-pole": "Tri-Pole", "Allpass": "Allpass",
    "Comb +": "Comb + and Comb", "Comb -": "Comb + and Comb", "Sample & Hold": "Sample & Hold",
}
FX_ALIAS = {"Freq Shift": "Frequency Shifter", "Ring Mod": "Ring Modulator",
            "Rotary": "Rotary Speaker", "Audio In": "Audio Input"}
OSC_ALIAS = {"Audio In": "Audio Input"}


def _split_filter(svalue: str) -> tuple[str, str]:
    """"LP Vintage Ladder" → ("LP", "Vintage Ladder"); a name with no known mode → ("", name)."""
    head, _, rest = svalue.partition(" ")
    return (head, rest) if head in _MODES and rest else ("", svalue)


def _search_int(xml: str, pattern: str) -> int | None:
    m = re.search(pattern, xml)
    return int(m.group(1)) if m else None


def _read_fxp(path: str) -> tuple[list[str], str]:
    """Audible oscillator type names + comment from a patch .fxp (both absent → ([], ""))."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return [], ""
    i, j = data.find(b"<?xml"), data.find(b"</patch>")
    if i < 0 or j < 0:
        return [], ""
    xml = data[i:j + len(b"</patch>")].decode("utf-8", "replace")

    m = re.search(r'<meta\b[^>]*\bcomment="([^"]*)"', xml)
    comment = html.unescape(m.group(1)).strip() if m else ""

    # Scene A always sounds; scene B only when not in Single mode (scenemode 0).
    scenes = ["a"] + (["b"] if (_search_int(xml, r'<scenemode\b[^>]*value="(\d+)"') or 0) else [])
    oscs: list[str] = []
    for sc in scenes:
        for k in (1, 2, 3):
            t = _search_int(xml, rf'<{sc}_osc{k}_type\b[^>]*value="(\d+)"')
            if t is None or not (0 <= t < len(_OSC_NAMES)):
                continue
            muted = _search_int(xml, rf'<{sc}_mute_o{k}\b[^>]*value="(\d+)"')
            if muted:  # mute param present and == 1
                continue
            oscs.append(OSC_ALIAS.get(_OSC_NAMES[t], _OSC_NAMES[t]))
    return list(dict.fromkeys(oscs)), comment


def read_patches(db_path: str = DEFAULT_DB, patch_scope: str = "all") -> list[dict]:
    """Read the Surge patch library into raw `kind:"patch"` records. `patch_scope`:
    "all" (factory + 3rd-party) or "factory". Returns [] if the DB is absent."""
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path)

    feats: dict[int, dict] = defaultdict(
        lambda: {"AUTHOR": "", "TAG": [], "FILTER": [], "FX": []})
    for pid, feature, sval in con.execute(
            "SELECT patch_id, feature, feature_svalue FROM PatchFeature"):
        f = feats[pid]
        if feature == "AUTHOR":
            f["AUTHOR"] = sval
        elif feature in ("TAG", "FILTER", "FX"):
            f[feature].append(sval)

    q = "SELECT id, path, name, category FROM Patches"
    if patch_scope == "factory":
        q += " WHERE path LIKE '%patches_factory%'"
    q += " ORDER BY id"  # deterministic order (content-addressing stability)

    records: list[dict] = []
    for pid, path, name, category in con.execute(q):
        f = feats.get(pid, {})
        oscs, comment = _read_fxp(path)
        records.append({
            "kind": "patch", "path": path, "name": name,
            "category": (category or "").split("/")[-1] or "Uncategorized",
            "author": f.get("AUTHOR", ""),
            "tags": list(dict.fromkeys(f.get("TAG", []))),
            "filters": [_split_filter(s) for s in dict.fromkeys(f.get("FILTER", []))],
            "effects": list(dict.fromkeys(f.get("FX", []))),
            "oscillators": oscs, "comment": comment,
        })
    con.close()
    return records
