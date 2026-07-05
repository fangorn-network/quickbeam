# Writing a scraper `Source`

Implement a data source once; get the whole ingest runtime for free — the CLI,
staged-volume emission, incremental checkpointing, a `--watch` daemon, and
`--publish` to fangorn. This guide shows how in ~40 lines. Background + rationale:
[SCRAPER_HARNESS.md](SCRAPER_HARNESS.md).

## The idea

The harness (`quickbeam.ingest.scrapers.harness`) owns everything generic. You supply
a `Source` — three methods and a few attributes — and it slots into
`quickbeam data <your-verb>` with the full loop. The whole interface between your
scraper and the rest of the system is two data shapes:

```
node  = {"name": <stable id>, "fields": {... , "text": <the blurb that gets embedded>}}
edge  = {"rel": <relation>, "from": <node name>, "to": <node name>,
         "fromType": <entity>, "toType": <entity>}
```

`build_graph` returns `({entity_type: [node, ...]}, [edge, ...])`. That's it.

## The contract

```python
# quickbeam/ingest/scrapers/mysrc.py
import argparse
from .harness import run_source
from .source import SourceBase


class MySource(SourceBase):
    name = "mysrc"                      # log/checkpoint-key prefix (need not equal the CLI verb)
    stems = {"Widget": "widgets"}       # entity_type → volume stem (default: entity.lower())
    snapshot_stems = {"widgets"}        # stems replaced wholesale each run (vs. ledgered under --accumulate)
    role_map = {"title": "title", "tags": ["kind"], "text": ["text"]}   # --dry-run preview
    presentation = {"accent": "#888", "icons": {"Widget": "widgets"}}   # display metadata

    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        # Your source-only flags. The shared ones (--output-dir/--volume/--watch/
        # --poll-interval/--accumulate/--checkpoint-file + the publish group) are already added.
        p.add_argument("--api-url", default="https://example.com/api")

    def read(self, cursor: int, args: argparse.Namespace) -> list[dict]:
        # All network/DB IO here. `cursor` is the persisted checkpoint (0 if none);
        # combine it with your own floor flags to decide what to (re-)emit.
        return fetch_widgets(args.api_url, since=cursor)

    def build_graph(self, records: list[dict]) -> tuple[dict, list]:
        # Pure. records → (nodes_by_type, edges). This is the unit-testable core.
        nodes = {"Widget": [{"name": f"widget:{r['id']}",
                             "fields": {"title": r["name"], "kind": r["kind"],
                                        "text": f"{r['name']} — a {r['kind']} widget"}}
                            for r in records]}
        return nodes, []

    def next_cursor(self, records: list[dict], prev: int) -> int:
        # The checkpoint to persist after this cycle. Return `prev` if nothing advances it.
        return max([prev, *(r["block"] for r in records)]) if records else prev


def run() -> None:
    run_source(MySource())
```

## Required vs. optional

**Required:** `name`, `snapshot_stems`, `role_map`, `presentation`, `stems`, and the four
methods (`add_source_args`, `read`, `build_graph`, `next_cursor`). Subclassing
`SourceBase` gives every attribute a default so you only set what differs.

**Optional attributes** (harness reads via `getattr`):

| attribute | default | when to set |
|---|---|---|
| `default_volume` | `1` | your source coexists at a fixed volume (events → `2`) |
| `edges_stem` | `"edges"` | you want a prefixed edges file (osm → `"osm_edges"`) |

## Two source shapes

**Live-tail** (robinhood): `read` uses `cursor` as an incremental floor; `next_cursor`
advances it (e.g. the max block seen). `--watch` polls; `--checkpoint-file` persists the
cursor; `--accumulate` grows a ledger of the non-snapshot stems.

**Batch** (events, osm, places): a static source with no tail. Return `prev` from
`next_cursor` and it "just works" — the cursor never advances, `--watch` harmlessly
re-runs the batch, and every stem is a snapshot (`snapshot_stems = set(stems.values())`).
No special-casing needed.

## `build_graph` may close over side inputs

`build_graph`'s signature is `(records) → (nodes, edges)`, but it may read *config for
the shaping* that `read` stashes on `self` — e.g. events' business index for the
`hostedAt` merge link, or osm/places' `near`-radius:

```python
def read(self, cursor, args):
    self._near_radius = args.near_radius_m       # stashed config, not a raw record
    return fetch(...)

def build_graph(self, records):
    radius = getattr(self, "_near_radius", 0.0)  # read back with a default
    ...
```

Keep raw data in `records` and shaping *config* on the instance. This keeps the
contract stable and `build_graph` trivially testable (set the attr, call it).

## Register it

Add one entry point so `discover_sources()` finds it — the CLI verb is the entry-point
name (it can differ from `Source.name`):

```toml
# pyproject.toml  (in-tree; a third-party package uses the same group)
[project.entry-points."quickbeam.sources"]
mysrc = "quickbeam.ingest.scrapers.mysrc:MySource"
```

For in-tree sources also add the same line to `_BUILTIN` in
`quickbeam/ingest/scrapers/__init__.py` so it works before a reinstall. A third-party
pip package needs **only** the entry point — `quickbeam data mysrc` then works with the
full watch/publish/checkpoint loop, zero changes to this repo.

## Test it

Because `build_graph` is pure, a test is: hand-build `records`, call it, assert on
nodes/edges. See `scrapers/test_events.py` / `test_osm.py` / `test_places.py` for the
pattern (normalize/shape assertions + a graph-builder assertion + a `next_cursor`
no-op check). No network, no DB.

```python
def test_build_graph():
    src = MySource()
    nodes, edges = src.build_graph([{"id": 1, "name": "A", "kind": "gadget"}])
    assert nodes["Widget"][0]["name"] == "widget:1"
```

## When NOT to use the harness

The harness holds the full node/edge set in memory (`emit_volumes`) and assumes one raw
record stream feeds `build_graph`. Two things it deliberately does **not** fit:

- A **streaming relational exporter** built for scale, where nodes and edges come from
  separate passes and never fit in memory (`pipelines/mb_pg.py` — server-side Postgres
  cursors straight to per-entity files). Keep it standalone.
- A **raw extractor / crawler** that fetches upstream data into Postgres or `.jsonl` but
  does no graph shaping (`pipelines/places.py`, `events.py`, `lastfm.py`). That's the
  *extract* stage that runs **before** a Source's `read`. A Source is a *shaper*.
