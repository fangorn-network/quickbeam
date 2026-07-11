# Publishing to Fangorn with quickbeam — the developer guide

Go from **"I have a scraper"** to **data live on Fangorn** in a few lines of Python.

quickbeam is a **framework, not a data set**: it ships *no* sources of its own. You bring
a scraper, and quickbeam automates everything between it and Fangorn — shaping records into
a graph, inferring the schemas, bootstrapping the on-chain repo, staging, committing, and
pushing. This guide shows the whole path. For the `Source` contract itself in depth, see
[`SCRAPER_HARNESS_AUTHORING.md`](./SCRAPER_HARNESS_AUTHORING.md); for a complete, runnable
project, see the sibling [`quickbeam-publisher`](../../quickbeam-publisher/) example.

---

## Mental model

```
   your scraper            quickbeam (the framework)                    Fangorn
 ┌──────────────┐   read   ┌───────────────────────────────┐  commit   ┌──────────┐
 │  Source      │ ───────► │  Publisher                    │ ────────► │ on-chain │
 │  .read()     │  records │   ingest → onboard → publish  │   +push   │  dataset │
 │  .build_graph│          │   (harness + schemagen +      │           └──────────┘
 │  .next_cursor│          │    fangorn CLI)               │
 └──────────────┘          └───────────────────────────────┘
```

- **You write** a `Source`: `read` (pull raw records), `build_graph` (shape them into
  typed nodes + edges), `next_cursor` (checkpoint), and a few descriptive attributes.
- **quickbeam owns** the rest: staging volumes, incremental checkpointing, schema
  inference, repo bootstrap, and the on-chain commit/push — via one class, `Publisher`.

The unit of exchange is two plain-dict shapes (nothing quickbeam-specific leaks into your
scraper):

```python
node = {"name": <stable id>, "fields": {..., "text": <the blurb that gets embedded>}}
edge = {"rel": <relation>, "from": <node name>, "to": <node name>,
        "fromType": <entity>, "toType": <entity>}
```

---

## 1. Install

quickbeam is the framework dependency; your scraper lives in your own project.

```bash
python -m venv .venv && source .venv/bin/activate

# Depend on quickbeam (editable install from the repo, or a published wheel):
pip install -e '/path/to/embeddings[cpu]'      # or [gpu] for CUDA embeddings

# One-time Fangorn credential setup (needed only to publish on-chain):
fangorn init
```

Your `pyproject.toml`:

```toml
[project]
dependencies = ["quickbeam @ file:///path/to/embeddings"]
```

---

## 2. Write a Source

A `Source` is your scraper adapted to the two-shape contract. Subclass `SourceBase` for
attribute defaults and implement four methods. Complete minimal example (Hacker News):

```python
# my_pkg/hackernews.py
import argparse, json, urllib.request
from quickbeam import SourceBase

HN = "https://hacker-news.firebaseio.com/v0"
def _get(p): return json.loads(urllib.request.urlopen(f"{HN}/{p}.json", timeout=30).read())

class HackerNewsSource(SourceBase):
    name = "hackernews"                                    # log / checkpoint-key prefix
    stems = {"Story": "stories", "Author": "authors"}      # entity → volume file stem
    snapshot_stems = {"stories", "authors"}                # replaced wholesale each run
    role_map = {"title": "title", "subtitle": "author",    # drives the --dry-run preview
                "tags": ["kind"], "text": ["text"]}
    presentation = {"accent": "#ff6600",                   # display metadata (accent + icons)
                    "icons": {"Story": "article", "Author": "person"}}

    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--limit", type=int, default=30)    # YOUR flags; harness adds the rest

    def read(self, cursor: int, args) -> list[dict]:       # all network/DB IO lives here
        ids = _get("topstories")[: args.limit]
        return [s for s in (_get(f"item/{i}") for i in ids) if s and s.get("title")]

    def build_graph(self, records):                        # PURE: records → (nodes, edges)
        stories, authors, edges, seen = [], [], [], set()
        for s in records:
            sid, by = f"hn:story:{s['id']}", s.get("by")
            stories.append({"name": sid, "fields": {
                "entityType": "Story", "title": s["title"], "author": by,
                "kind": s.get("type", "story"),
                "text": f"{s['title']}. {s.get('score', 0)} points."}})
            if by:
                if by not in seen:
                    seen.add(by)
                    authors.append({"name": f"hn:author:{by}", "fields": {
                        "entityType": "Author", "title": by, "text": f"HN user {by}."}})
                edges.append({"rel": "postedBy", "from": sid, "to": f"hn:author:{by}",
                              "fromType": "Story", "toType": "Author"})
        return {"Story": stories, "Author": authors}, edges

    def next_cursor(self, records, prev: int) -> int:
        return prev                                        # batch source — no live tail
```

That's the whole scraper. See [`SCRAPER_HARNESS_AUTHORING.md`](./SCRAPER_HARNESS_AUTHORING.md)
for the required-vs-optional attribute table, the two source shapes (live-tail vs batch),
and how `build_graph` may read shaping config stashed by `read`.

---

## 3. Publish from Python — the `Publisher`

`quickbeam.Publisher` binds your `Source` to a Fangorn repo and runs the whole loop.

```python
import quickbeam as qb
from my_pkg.hackernews import HackerNewsSource

pub = qb.Publisher(
    HackerNewsSource(),
    repo="./hn-front",          # the Fangorn repo dir (holds .fangorn/); created by onboard
    prefix="me.hn",             # schema-name prefix → <prefix>.<type>.<version>
    bundle_name="frontpage",    # the repo's bundle schema → <prefix>.<bundle_name>.<version>
)

pub.run(limit=30)               # onboard (first time) → ingest → commit + push
```

`run(**kwargs)` forwards `kwargs` to your source's flags — they're the argparse *dests*
(`limit=`, `place=`, `with_transfers=`, `accumulate=`, `dry_run=` …). A typo raises rather
than being silently ignored.

### The three legs, individually

You can drive each step yourself when you want control:

```python
pub.ingest(limit=30, dry_run=True)   # read + shape → preview only (writes nothing)
pub.ingest(limit=30)                 # read + shape → stage volume_<n>_*.json  (+ checkpoint)
pub.onboard()                        # infer schemas + `fangorn repo init`  (idempotent)
pub.publish(message="morning pull")  # `fangorn commit --bundle` (auto-registers) + `push`
```

Every method returns `self`, so `pub.ingest(...).publish()` chains.

### Constructor reference

| arg | default | meaning |
|---|---|---|
| `source` | — | your `Source` instance |
| `repo` | `"."` | Fangorn repo dir (`.fangorn/` lives here) |
| `prefix` | — | schema-name prefix |
| `bundle_name` | — | bundle stem (the repo's schema) |
| `version` | `"v1"` | schema version tag |
| `output_dir` | `<repo>/stage_volumes` | where staged volumes are written |
| `volume` | `source.default_volume` or `1` | volume number |
| `fangorn_bin` | `"fangorn"` | fangorn CLI invocation (shell-split; pass a full command for a dev build) |

---

## 4. What each leg actually does

**`ingest(**kwargs)`** → calls your `read()` with the persisted checkpoint, runs
`build_graph()`, and writes `volume_<n>_<stem>.json` node/edge files under `output_dir`,
then advances the checkpoint. `dry_run=True` prints the shaped nodes and the exact text
that would be embedded, and writes nothing. `accumulate=True` grows a *ledger* (merges new
rows into the staged files) instead of replacing them — pair with `checkpoint_file=` for a
resumable live tail.

**`onboard()`** → infers the schemas from the staged volumes (one `SchemaDefinition` per
node type + one bundle spanning them) and writes them to `<output_dir>/schemas/`, then runs
`fangorn repo init <repo-name> -s <bundleSchema>`. The bundle schema's id is deterministic
from its name, so this works *before* the schema is registered on-chain. It's idempotent —
it skips `repo init` when `<repo>/.fangorn` already exists (pass `force=True` to redo it).
If nothing is staged yet, pass ingest kwargs so it can sample: `onboard(limit=30)`.

**`publish()`** → `fangorn commit --bundle <output_dir>` (which **auto-registers** any
missing schemas from `<output_dir>/schemas/` — there is no separate registration step) +
`fangorn push`. Requires an onboarded repo.

> **One repo, one bundle schema.** Publish two different datasets (say OSM places and
> Robinhood markets) to **separate repos** — two `Publisher`s with distinct `repo=` dirs.

---

## 5. Register as a CLI command (optional)

Handing your `Source` to `Publisher` needs no registration. But if you also want a
`quickbeam data <verb>` CLI command (with the full `--watch` / `--publish` / `--dry-run`
loop for free), add one entry point in **your** package's `pyproject.toml`:

```toml
[project.entry-points."quickbeam.sources"]
hackernews = "my_pkg.hackernews:HackerNewsSource"
```

quickbeam core registers none of its own — `discover_sources()` returns exactly the sources
contributed by installed packages via this `quickbeam.sources` group. After `pip install`:

```bash
quickbeam data hackernews --dry-run
quickbeam data hackernews --limit 50 --publish --repo ./hn-front
```

The CLI verb (`hackernews`) is the entry-point *name* and can differ from `Source.name`.

---

## 6. End-to-end, on the command line

The same lifecycle without Python, once your source package is installed:

```bash
# 1. stage volumes from your source
quickbeam data hackernews --limit 30 --output-dir ./hn-front/stage_volumes

# 2. infer schemas (writes ./hn-front/stage_volumes/schemas/)
quickbeam data schemagen --input-dir ./hn-front/stage_volumes \
    --prefix me.hn --bundle-name frontpage

# 3. bootstrap the repo against the bundle schema (once)
cd hn-front && fangorn repo init hn-front -s me.hn.frontpage.v1

# 4. commit (auto-registers schemas) + push
fangorn commit --bundle ./stage_volumes -m "hn front page" && fangorn push
```

`quickbeam data <verb> --publish` collapses 1–4 into one command once the repo exists.

---

## 7. Worked examples

The [`quickbeam-publisher`](../../quickbeam-publisher/) project publishes three sources,
each defined locally in its `qb_sources/` package (importing only the quickbeam framework):

| source | shape | driver |
|---|---|---|
| **hackernews** | batch, no-key HTTP API | `python -m qb_sources.hackernews` |
| **robinhood** | live-tail on-chain (Blockscout) | `python publish_robinhood.py` |
| **osm** | batch spatial (Overpass) | `python publish_osm.py` |

```python
# publish_robinhood.py  — live tail with real transfer flow
import quickbeam as qb
from qb_sources.robinhood import RobinhoodSource

qb.Publisher(RobinhoodSource(), repo="./rh-market",
             prefix="me.rh", bundle_name="market") \
  .run(with_transfers=True, max_transfers=200)
```

---

## 8. Prerequisites & troubleshooting

- **`fangorn init` first.** `onboard`/`publish` shell out to the fangorn CLI; without
  configured credentials `repo init` fails. Publishing (`fangorn push`) is the
  permissioned, on-chain step.
- **`unknown ingest argument(s) [...]`** — a kwarg to `ingest`/`run` didn't match any of
  your source's flags (or the shared harness flags). Check the dest name (underscored:
  `--with-transfers` → `with_transfers`).
- **`onboard: no staged volumes`** — call `ingest(...)` first, or pass ingest kwargs to
  `onboard(...)` so it can sample your source for schema inference.
- **`quickbeam data <verb>` not found** — the source package isn't installed in this
  environment. `discover_sources()` is plugin-driven; install your package (its entry
  point) into the same venv.
- **Dev fangorn build** — pass the full invocation as `fangorn_bin`, e.g.
  `Publisher(..., fangorn_bin="dotenvx run -f ~/fangorn/fangorn/.env -- node ~/fangorn/fangorn/dist/src/cli/cli.js")`.
- **Postgres-backed sources** (like the example's `events`/`places`) read from a DB that a
  separate *extract* stage fills first — the `Source` is a shaper, not the crawler.

---

## Where to go next

- [`SCRAPER_HARNESS_AUTHORING.md`](./SCRAPER_HARNESS_AUTHORING.md) — the `Source` contract
  in depth: required/optional attributes, live-tail vs batch, testing a pure `build_graph`.
- [`NEW_QUICKSTART.md`](./NEW_QUICKSTART.md) — the git-native Fangorn flow (`commit`/`push`/
  `log`/`status`) your published data rides on.
- [`quickbeam-publisher/README.md`](../../quickbeam-publisher/README.md) — the runnable
  example project.
