# quickbeam-surgext

A pluggable [quickbeam](../../../) `Source` that turns the **Surge XT user manual** (a PDF) **and the
installed Surge patch library** into a rich [Fangorn](../../../../fangorn) knowledge graph — searchable
by natural language and walkable as a mesh.

It follows the same pattern as [`example-robinhood-source`](../../sherwood/example-robinhood-source): the source
supplies only *read + shape + cursor*; the quickbeam harness owns the CLI, staged-volume emission,
`--dry-run`, and `--publish` to Fangorn. Installing this package registers a `quickbeam.sources`
entry point, so `quickbeam data surgext` appears automatically.

## What it produces

A deterministic graph (no LLM → re-runs are byte-identical, so Fangorn's content-addressing shares
unchanged blocks across commits): **~3,595 vertices / ~16,900 edges.**

| Entity type | From | Count |
|---|---|---|
| `Section` / `Module` | manual headings (font-size hierarchy) | 154 / 6 |
| `Effect` | Technical-Reference FX sections | 29 |
| `OscillatorType` | oscillator-algorithm sections | 12 |
| `FilterType` | the "Filter Types" list | 14 |
| `ModulationSource` | LFO shapes + internal modulators | 14 |
| `Parameter` | effect/module parameter tables | 356 |
| `Patch` | factory + 3rd-party presets | 3,010 |

Relations: `hasSubsection`, `hasParameter`, `hasType`, `providesModulation`, `seeAlso` (from the
manual) and `usesFilter`, `usesEffect`, `usesOscillator` (patches → manual entities).
`fields.text` is the embedded document.

## Install

```bash
pip install -e .        # into an env that also has `quickbeam`
```

Needs `pymupdf` (PDF parsing) — pulled in as a dependency. Reading patches needs Surge XT installed
(the patch DB at `~/Documents/Surge XT/SurgePatches.db` and the patch files under `/usr/share/surge-xt`).

## Usage

```bash
# Preview the shaped graph (no Qdrant, no chain):
quickbeam data surgext --pdf-path ./Surge-XT-Manual.pdf --dry-run

# Stage the node/edge volumes to disk:
quickbeam data surgext --pdf-path ./Surge-XT-Manual.pdf --output-dir ./stage_volumes

# Publish on-chain (needs a registered wallet + USDC + storage subscription):
quickbeam data surgext --pdf-path ./Surge-XT-Manual.pdf --publish --namespace surgext \
    --fangorn-bin "<your fangorn CLI invocation>"
```

Source-specific flags: `--pdf-path` (default `./Surge-XT-Manual.pdf`), `--patch-db`
(default `~/Documents/Surge XT/SurgePatches.db`; empty/absent → manual only), `--patch-scope`
(`all` = factory + 3rd-party, or `factory`). The shared harness flags (`--dry-run`, `--output-dir`,
`--volume`, `--publish`, `--namespace`, `--fangorn-bin`) come from quickbeam.

Then embed + search with the usual quickbeam commands (`quickbeam data prebake` for an offline
build into Qdrant, or `quickbeam build`/`serve`/`mcp` off a published namespace).

## How it works

- **`extract.py`** — PDF → raw records via PyMuPDF. The manual is a Chrome-printed PDF with a clean
  font-size heading hierarchy (22.5=H1, 18=H2, 15=H3, 13.5=H4, 12=body, 8=footer), so structure is
  recovered deterministically. Parameter tables come from `find_tables()` for *structure* with cell
  *text* pulled via `get_text(clip=…)` (see the gotcha below).
- **`patches.py`** — the second data source. Filter/FX usage comes as decoded strings from Surge's
  own `SurgePatches.db` `PatchFeature` table (no enum work); oscillator usage + comments are read
  from each `.fxp` (the osc-type enum was taken from the installed Surge binary's `osc_type_names`),
  linking only *audible* oscillators.
- **`source.py`** — `build_graph(records)` (pure, testable) turns records into typed vertices +
  edges, and back-fills patch vocabulary (categories, `lowpass`/`highpass` modes, example patch
  names) into the filter/effect/oscillator nodes' text — the faithful fix for "the manual is
  technical, users search perceptually."

## Gotchas

- **PyMuPDF `find_tables().extract()` mangles this PDF's text** (per-ligature Type3 sub-fonts get
  reordered: "distortion" → "distortoi n"). Plain text / `get_text("words")` / clip-text are clean.
  `extract.py` takes table structure from `find_tables()` but cell text via `get_text(clip=…)` —
  **do not switch to `.extract()`.**
- The oscillator enum (`0 Classic … 11 Alias`) is baked from Surge 1.3.4's binary; verify if the
  Surge version changes. An oscillator is linked only if audible (scene active via `scenemode` +
  its mixer channel not muted) — otherwise every patch's muted default Classic osc3 would link
  everywhere.
- **Faithful finding:** "warm = ladder filter" is synth folklore the factory data does *not*
  support ("Warm"-named patches use a spread of filters), so the back-fill correctly does not force
  a ladder to rank for "warm". Don't add tonal synonyms by hand — that would editorialize beyond
  the manual.

## Figures & on-chain images

`--image-dir` extracts the manual's figures (75 screenshots + 44 rendered block diagrams) as
content-hash PNGs and records `fields.images = [{file, w, h, kind}]` on each section (kept out of
`fields.text`, so embeddings are unchanged).

To make the figures **travel with the on-chain graph** (so any Fangorn consumer can resolve them,
not just your CDN), pin them to IPFS and fold the CIDs into the graph:

```bash
# 1. extract figures
quickbeam data surgext --pdf-path ./Surge-XT-Manual.pdf --image-dir ./images --output-dir ./stage
# 2. pin them to IPFS (Pinata — the same storage the SDK uses); writes images/image-cids.json
surgext-pin-images --image-dir ./images --pinata-jwt "$PINATA_JWT"
# 3. re-extract (now folds cid into each fields.images[]) and publish
quickbeam data surgext --pdf-path ./Surge-XT-Manual.pdf --image-dir ./images \
    --publish --namespace surgext --fangorn-bin "<fangorn CLI>"
```

Each image becomes a content-addressed IPFS blob referenced by `fields.images[].cid` — the standard
IPLD "reference big blobs by CID" pattern, keeping the graph lean while the bytes live on IPFS. A
consumer resolves `<gateway>/ipfs/<cid>`. The off-chain website is independent of this: it serves
figures from a private same-origin bundle (see `../manual`), so pinning is only needed for on-chain
portability.

## Tests

```bash
pytest tests/     # build_graph purity + determinism, extraction smoke, patch reader (real DB)
```
