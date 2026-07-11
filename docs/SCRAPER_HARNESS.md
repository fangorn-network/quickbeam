# Scraper Harness plan

Turn the `quickbeam data <name>` pipelines from a set of standalone, copy-pasted
scripts into **implementations of one shared ingestion contract**. A scraper author
writes a thin `Source` (read + shape + cursor); the harness owns everything else —
argparse, volume emission, checkpointing, the watch daemon, and publish-to-fangorn.
The goal is that ingestion becomes a *pluggable data product*: a new source (in-tree
or shipped as a third-party pip package) drops in and gets the full incremental
watch/publish loop for free.

## Progress

- ✅ **Step 1–3 (done)** — `quickbeam/ingest/scrapers/` scaffolded with `source.py`
  (the `Source` Protocol + `SourceBase`), `harness.py` (the source-agnostic runtime:
  shared CLI, `emit_volumes`, checkpointing, watch daemon, publish), and
  `robinhood.py` (`RobinhoodSource` — the shaper + read only). `pipelines/robinhood.py`
  is now a back-compat shim re-exporting the public surface (incl. legacy
  `emit_volumes`/`compose_searchable_text` wrappers) so `cli.py` and the tests import
  unchanged. **Verified:** `py_compile` clean; `test_robinhood.py` 11/11 green;
  `data robinhood --help` shows all 21 flags; an offline golden baseline (build_graph +
  compose text + emit counts/files over a fixed event set) is **byte-identical**
  before/after.
- ✅ **Step 4 (done)** — ported **two** more sources, `events` (`EventsSource`) and
  `osm` (`OsmSource`), onto the harness; `pipelines/events_pg.py` + `pipelines/osm.py`
  are now shims. Both are **batch** sources (no live tail) — this is where the contract
  got stress-tested and lightly bent (see *What porting revealed* below). **Verified:**
  `py_compile` clean; robinhood 11/11 still green; all three `data {osm,eventspg,robinhood}
  --help` parse; a fixture golden captured from each *old* pipeline (events via `--raw-in`;
  osm via monkeypatched Overpass/Wikidata) is **parsed-identical** through the new
  `read → build_graph → emit_volumes` path; and a real `quickbeam data eventspg --raw-in`
  CLI run exercises the full harness end-to-end.
- ✅ **Step 5 (done)** — source **discovery**. `discover_sources()` merges an in-tree
  registry (`_BUILTIN`) with any `quickbeam.sources` entry points; `cli.py`'s `data`
  sub-app now generates one `data <verb>` command per discovered source instead of three
  hand-written stanzas. Entry points declared in `pyproject.toml`. **Verified:** all
  three verbs appear in `data --help`, parse their own flags through passthrough, and run
  end-to-end; after `pip install -e .` the entry points register in metadata and load via
  the `ep.load` path (proving the third-party mechanism, not just the built-in fallback);
  the non-source `data` commands (schemagen, placespg, linkgen, prebake, …) are untouched.
  A third-party pip package now gets `quickbeam data <its-name>` with the full
  watch/publish/checkpoint loop by adding one entry point — zero changes to this repo.
- ✅ **Step 6 (done)** — authoring guide at
  [SCRAPER_HARNESS_AUTHORING.md](SCRAPER_HARNESS_AUTHORING.md): "a Source in ~40 lines,"
  required/optional attributes, live-tail vs. batch, the side-inputs-on-`self` pattern,
  registration, testing, and a *when NOT to use the harness* section.
- ✅ **Step 7 (done, scoped honestly)** — ported **`places_pg` → `PlacesSource`** (a clean
  twin of events; golden parsed-identical; shim + `test_places.py` added; discovered as
  `data placespg`). **Deliberately did NOT port `mb_pg` or `lastfm`** — see below; forcing
  them in would be wrong. Five sources now run on the harness: robinhood, osm, events,
  places (shapers) — and the discovery/CLI is source-driven.

Committed unit tests for every ported shaper (the gap flagged earlier is closed):
`test_events.py` (8) + `test_osm.py` (9) + `test_places.py` (5), joining
`test_robinhood.py` (11) → **33 green**.

### The harness boundary (step 7 finding)

Porting the remaining pipelines drew a clear line around what the harness is *for*. It
fits an **in-memory shaper fed by one raw-record stream**. Two pipelines are outside
that and stay standalone — this is a feature, not a gap:

- **`mb_pg`** — a *streaming relational exporter* built for the full multi-million-row
  MusicBrainz Postgres DB. Nodes and edges come from **separate** SQL passes via
  server-side cursors streamed straight to per-entity files; there is no unified
  `records` list and it must never materialize in memory. The harness's `emit_volumes`
  (which holds the whole node/edge set) is the wrong tool.
- **`lastfm`** (and `places.py` / `events.py`) — *raw extractors / crawlers* that fetch
  upstream data into Postgres or `.jsonl`, doing no graph shaping. That is the *extract*
  stage that runs **before** a Source's `read`. A Source is a *shaper*, so these are not
  Sources by definition.

The authoring guide's "When NOT to use the harness" section documents this so the next
contributor doesn't try to force either pattern in.

### What porting revealed (contract findings)

The two batch sources validated the contract but forced three small, general
extensions to the harness (all backward-compatible, robinhood unaffected):

1. **Per-source `edges_stem` + `default_volume`** (read via `getattr`, defaults
   `"edges"` / `1`). OSM writes `volume_N_osm_edges.json` and events defaults to
   volume 2 — both are now source attributes, not hardcoded.
2. **`read → build_graph` split is the real work.** Both old pipelines interleaved
   fetch + shape + write in one `run_export`. Splitting them (network/DB IO + the
   layer-order dedup in `read`; pure shaping in `build_graph`) is behaviour-preserving
   and makes the shaper unit-testable — a genuine improvement, not just relocation.
3. **`build_graph` may close over side inputs.** events needs a business index (for
   `hostedAt`), osm needs the near-radius + default locality. These are *config for
   the shaping*, not raw records, so `read` stashes them on the instance
   (`self._businesses`, `self._near_radius`, …) and `build_graph` reads them. The
   contract stays `(records) → (nodes, edges)`; the closure is documented, not widened.

Resolved open questions: **int cursor is enough** (batch sources just return `prev`);
**`--watch` on a static source is a harmless no-op** (re-runs the batch, cursor never
advances, no special-casing needed); **multi-volume is the `--volume` flag** (+ the new
`default_volume`), not a Source property.

## Why now

`quickbeam/pipelines/robinhood.py` (1039 lines) has independently grown a complete
ingestion runtime — incremental block-cursor checkpointing, `--accumulate` ledger
merge, stale-volume pruning, a `--watch` poll daemon, `--publish` to fangorn, and a
`--dry-run` embed-text preview. Roughly **75% of that file is source-agnostic** and
duplicated in weaker form across `places_pg` / `events_pg` / `mb_pg` / `osm`. It is
the framework, fused into its first concrete instance. This plan extracts it.

This is the **scrape/shape** counterpart to `REFACTOR_PLAN.md`, which refactors the
**embed/load** side (`embeddings.py` → the `watch`/`build` engine). Both land under
`quickbeam/ingest/`; see *Relationship to the embeddings refactor* below.

## The contract (what every pipeline already satisfies)

Every `data <name>` command converges on the same output shape:

> read from a source → shape into `volume_{N}_{type}.json` node/edge files in a stage
> dir → (`schemagen` → `fangorn commit --bundle` → `push`) → `watch` embeds → CDN.

A **node** is `{name, type, fields}`; an **edge** is `{rel, from, to}`. That is the
whole interface between a scraper and the rest of the system. The harness formalizes
it as a `Source`.

## Decisions (locked)

- **Contract name:** `Source` — a Protocol, not a base class, so out-of-tree
  implementers don't inherit an import. Lives at `quickbeam/ingest/scrapers/source.py`.
- **Package location:** `quickbeam/ingest/scrapers/` for the harness + adapters.
  Deliberately NOT `ingest/sources/` — the embeddings refactor already claims that
  name for embed-side network acquisition (subgraph/IPFS). Scrape-side = `scrapers/`.
- **Behavior-preserving extraction:** `robinhood.py` is the test oracle. Its
  `pipelines/test_robinhood.py` (177 lines, unit-tests the pure shaper) must stay
  green through every step. The public `run()`/`_parse_args` surface `cli.py` calls
  is preserved via a one-line shim.
- **Registration:** discover sources via `[project.entry-points."quickbeam.sources"]`
  so in-tree and third-party scrapers register identically. `cli.py`'s 15 hand-written
  `data` stanzas collapse to a discovery loop.

## The `Source` protocol

```python
# quickbeam/ingest/scrapers/source.py
from typing import Protocol
import argparse

class Source(Protocol):
    name: str                       # "robinhood" — the `data <name>` verb + log prefix
    snapshot_stems: set[str]        # stems rewritten wholesale (latest wins) vs. accumulated
    role_map: dict                  # title/subtitle/tags/text field roles (dry-run preview)
    presentation: dict              # accent color + per-type icons (display metadata)

    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        """Register source-only flags (--rpc-url, --bbox, --dsn …). The harness has
        already added the shared flags (--output-dir/--volume/--watch/…)."""

    def read(self, cursor: int, args: argparse.Namespace) -> list[dict]:
        """Pull raw records with cursor as the incremental floor. Returns the shaped
        event list build_graph consumes. Network/DB IO lives here and nowhere else."""

    def build_graph(self, records: list[dict]) -> tuple[dict[str, list], list]:
        """records → ({entity_type: [node]}, [edge]). Pure; unit-testable."""

    def next_cursor(self, records: list[dict], prev: int) -> int:
        """The checkpoint value to persist after this cycle (e.g. max transfer block).
        Returns prev when nothing advances it — snapshot-only reads never move it."""
```

Optional hooks (harness provides sane defaults, source overrides if needed):

- `stem_for(entity: str) -> str` — the `_STEM` map (default: `entity.lower()`).
- `merge_key(stem: str)` — dedup key under `--accumulate` (default: node `name`;
  edges keyed on `(rel, from, to)`). Robinhood's is exactly today's behavior.
- `verbalize(record) -> str` — searchable-text composer for the `--dry-run` preview.

## What the harness owns

`quickbeam/ingest/scrapers/harness.py` — lifted almost verbatim from `robinhood.py`,
made source-parameterized:

- **Shared argparse** — `--output-dir --volume --dry-run --watch --poll-interval
  --accumulate --checkpoint-file` and the `publish` group (`--publish --repo
  --fangorn-bin --commit-message`). Today these are re-declared in every pipeline.
- **`emit_volumes(src, records, …)`** — `build_graph` → write `volume_{N}_{stem}.json`,
  snapshot-vs-ledger merge via `_merge_keep_order`, stale-volume pruning scoped to the
  source's own stems. (robinhood.py:494–551, generalized.)
- **Checkpointing** — atomic, merge-not-clobber JSON cursor keyed per source
  (`{name}IngestBlock`), so pointing two sources at one checkpoint file is safe.
  (robinhood.py:903–929.)
- **`_ingest_once`** — load cursor → `read` → `emit_volumes` → advance cursor via
  `next_cursor` → optional `_publish_to_fangorn`. (robinhood.py:932–966.)
- **`_watch_ingest`** — the poll daemon; per-cycle try/except so one bad read never
  kills the loop. (robinhood.py:969–988.)
- **`_publish_to_fangorn`** — `commit --bundle` + `push` subprocess, `.fangorn/`
  preflight, `--fangorn-bin` splitting. (robinhood.py:991–1035.)
- **`run_source(src)`** — the entrypoint each adapter's `run()` delegates to.

## Target layout

```
quickbeam/ingest/scrapers/
  __init__.py         # discover_sources() via entry points; re-exports run_source
  source.py    ~60    # the Source Protocol + optional-hook defaults
  harness.py   ~420   # shared argparse, emit_volumes, checkpoint, ingest_once,
                      #   watch daemon, publish — ALL source-agnostic
  robinhood.py ~340   # RobinhoodSource: read_robinhood_events + build_graph +
                      #   verbalize + role_map + snapshot_stems  (was 1039 lines)
  osm.py / events.py / places.py / …  # ported incrementally
```

`pipelines/robinhood.py` stays as a thin back-compat shim: it re-exports the public
surface (`run`, the shaper fns, constants) plus legacy-signature `emit_volumes` /
`compose_searchable_text` wrappers bound to `RobinhoodSource`, so `cli.py` and the
tests keep importing from it unchanged.

## Registration & discovery

```toml
# pyproject.toml
[project.entry-points."quickbeam.sources"]
robinhood = "quickbeam.ingest.scrapers.robinhood:RobinhoodSource"
osm       = "quickbeam.ingest.scrapers.osm:OsmSource"
```

`cli.py`'s `data` sub-app builds its commands by iterating `discover_sources()`
instead of hand-declaring each one. A third party then ships `quickbeam-source-foo`
with its own entry point, and `quickbeam data foo --watch --publish` works with the
full loop — zero changes to this repo. **This is the actual developer-facing product.**

## Relationship to the embeddings refactor

Complementary, non-conflicting — both under `quickbeam/ingest/`:

| | `REFACTOR_PLAN.md` (embed/load) | this plan (scrape/shape) |
|---|---|---|
| Refactors | `embeddings.py` → engine subpackage | `pipelines/*` → `Source` adapters |
| Lands in | `ingest/` top-level + `ingest/graph/` + `ingest/sources/` | `ingest/scrapers/` |
| Driver | `watch` / `build` | `data <name>` |

**One collision to honor:** the embeddings plan uses `ingest/sources/` for embed-side
acquisition (The Graph + IPFS). This plan therefore uses `ingest/scrapers/`, never
`ingest/sources/`, to avoid two "sources" meanings in one package. The two refactors
can proceed in either order; they touch disjoint files.

## Execution order (each step ships independently)

1. ✅ **Scaffold** `ingest/scrapers/` + empty `__init__`; add `source.py` (Protocol +
   hook defaults). Nothing wired yet — pure addition.
2. ✅ **Extract harness** — move the source-agnostic plumbing out of `robinhood.py` into
   `harness.py`, parameterized by a `Source`. `test_robinhood.py` green.
3. ✅ **Define `RobinhoodSource`** — collapse the remaining robinhood.py to the adapter;
   `run()` becomes `run_source(RobinhoodSource())`. Leave a `pipelines/robinhood.py`
   shim. Full `data robinhood --dry-run` parity check.
4. ✅ **Port a second source** (`osm` **and** `events_pg`) onto the harness — validated
   the contract and forced three small harness extensions (see *What porting revealed*).
5. ✅ **Entry-point discovery** — `discover_sources()` (built-in registry + `quickbeam.sources`
   entry points); `cli.py`'s `data` sub-app is now a discovery loop. `quickbeam data --help`
   lists them; external packages register their own verb with no change here.
6. ✅ **Author guide** — `docs/SCRAPER_HARNESS_AUTHORING.md`: "a Source in ~40 lines,"
   with a *when NOT to use the harness* boundary section.
7. ✅ **Port the rest** — `places_pg` ported (clean twin of events); `mb_pg` + `lastfm`
   deliberately left standalone (streaming exporter / raw crawler — outside the harness's
   in-memory-shaper boundary; see *The harness boundary*).

## Verification gate (after every step)

- `py_compile` the package; import `quickbeam.cli`, `quickbeam.pipelines.robinhood`.
- `pytest quickbeam/pipelines/test_robinhood.py` (the pure-shaper oracle).
- `quickbeam data robinhood --help` parses; `--dry-run` produces identical node/edge
  counts + embed-text preview to pre-refactor (capture a baseline in step 1).

## Open questions — RESOLVED in step 4

- **Cursor generality.** ✅ `int` is enough. Both new sources are batch (no cursor) and
  return `prev` from `next_cursor`; robinhood's block height fits `int`. Not widening to
  an opaque cursor until a real source forces it.
- **Sources with no live tail.** ✅ Confirmed the clean path: `next_cursor → prev` means
  the checkpoint never advances, and `--watch` just re-runs the batch each cycle — a
  harmless no-op, no special-cased second code path in the harness.
- **Multi-volume sources.** ✅ It's the `--volume` flag plus the new per-source
  `default_volume` attribute (events → 2). The places↔events cross-link stays a separate
  `linkgen`/`keylink` concern, out of the harness.

Still open for later steps:

- **No test oracle for osm/events shapers.** robinhood has `test_robinhood.py`; the new
  sources were verified against a captured fixture golden but have no committed test.
  Worth adding `test_events.py` / `test_osm.py` (feed fixed records → assert nodes/edges)
  when porting settles — the `read/build_graph` split now makes this easy.
- **Dropped cosmetics.** The old `osm.main` deleted a stale `volume_N_osm_places.json`
  and printed a schemagen hint; both were pre-graph cruft and were not carried over.

## Out of scope (flagged, not doing)

- Merging the scrape-side and embed-side into one command. `data` (shape) and `watch`
  (embed) stay separate drivers — the on-chain commit sits between them by design.
- Reworking `schemagen` — it already reads whatever `volume_*.json` the harness emits.
- A plugin *sandbox*/trust model for third-party sources. Entry-point discovery runs
  arbitrary code; that's the same trust surface as any pip dependency. Note it,
  address it if/when there's an untrusted-source marketplace.
