"""The linkset — what fuses two sovereign graphs back into one.

Neither publisher can assert these edges alone: one endpoint lives in the
platform's graph, the other in the artist's. The linkset is plain data installed
into the CDN (`quickbeam cdn edges`), so the fusion needs no publish, no shared
database, and no code change on either side.

TWO ID SPACES — THE THING THAT WILL BITE YOU
--------------------------------------------
Edge endpoints must match the id the records were indexed under, and quickbeam has
two of those depending on how the graph reached Qdrant:

  offline (`quickbeam data prebake`) → the point id is the staged node NAME
                                       (`prebake.py:_load_nodes` → `str(rec["name"])`)
  on-chain (`quickbeam watch`)       → the point id is the IPLD vertex CID
                                       (`projection.py:project_source` passes
                                        `prefer_key="id"`, and that `id` is `v["cid"]`)

So the same logical linkset has to be emitted in whichever space is live. `--from`
selects: omitted → names (local iteration); given twice → read both published
namespaces back and rewrite every endpoint onto its CID.

CIDs only exist after a push, which is why this runs *after* both publishes.
"""
from __future__ import annotations

import argparse
import json
import sys

from .source import cross_edges, focus_id_of


def _key_from_name(name: str) -> tuple | None:
    """Node name → the identity that survives across id spaces.

    The artist stub and the artist's real node share an Audius id and an entity
    type, so the reference flag is part of the key — without it the `sameAs` would
    collapse onto itself.
    """
    if not name.startswith("audius:") or name.count(":") < 2:
        return None
    _, kind, ident = name.split(":", 2)
    entity = {"user": "Artist", "artistref": "Artist", "track": "Track",
              "playlist": "Playlist", "genre": "Genre", "mood": "Mood",
              "tag": "Tag"}.get(kind)
    return (entity, ident, kind == "artistref") if entity else None


def _key_from_vertex(v: dict) -> tuple | None:
    """Published vertex → the same identity."""
    p = v.get("payload") or {}
    ident = p.get("id")
    et = v.get("schemaId") or p.get("entityType")
    if not ident or not et:
        return None
    return (et, str(ident), bool(p.get("isReference")))


def cid_index(fangorn_bin: str, sources: list[str]) -> dict[tuple, str]:
    """key → vertex CID across every published namespace named in `--from`."""
    from quickbeam.ingest.sources.fangorn import parse_sources, read_source

    idx: dict[tuple, str] = {}
    for owner, ns in parse_sources(sources):
        snap = read_source(fangorn_bin, owner, ns)
        n = 0
        for v in snap.get("vertices", []):
            k = _key_from_vertex(v)
            if k:
                idx[k] = v["cid"]
                n += 1
        print(f"[link] {owner}:{ns} → {n} vertex CID(s)", file=sys.stderr)
    return idx


def to_cid_space(edges: list[dict], idx: dict[tuple, str]) -> tuple[list[dict], list[dict]]:
    """Rewrite endpoints onto CIDs. Returns (resolved, unresolved).

    Unresolved is not a warning to skim past: an edge whose endpoint isn't in the
    published graph is a dead end in the served CDN, so the count has to be zero.
    """
    ok, bad = [], []
    for e in edges:
        f, t = _key_from_name(e["from"]), _key_from_name(e["to"])
        fc, tc = idx.get(f), idx.get(t)
        if fc and tc:
            ok.append({**e, "from": fc, "to": tc})
        else:
            bad.append({**e, "missingFrom": None if fc else e["from"],
                        "missingTo": None if tc else e["to"]})
    return ok, bad


def build(records: list[dict], focus_handle: str) -> list[dict]:
    return cross_edges(records, focus_id_of(records, focus_handle))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="audius-link",
        description="Emit the cross-graph linkset fusing the platform graph and the "
                    "artist's own graph.")
    p.add_argument("--cache-file", default="./audius_cache.json",
                   help="The raw crawl cache both graphs were built from.")
    p.add_argument("--focus-artist", default="disclosure")
    p.add_argument("--out", default="./audius_linkset.json")
    p.add_argument("--from", dest="sources", action="append", default=[],
                   metavar="OWNER:NAMESPACE",
                   help="Pass TWICE (platform, artist) to rewrite endpoints onto "
                        "published vertex CIDs. Omit for the node-name space used by "
                        "the offline prebake path.")
    p.add_argument("--fangorn-bin", default="fangorn",
                   help="fangorn CLI command (may be a full shell command).")
    args = p.parse_args(argv)

    with open(args.cache_file, encoding="utf-8") as f:
        records = json.load(f)
    edges = build(records, args.focus_artist)

    rels: dict[str, int] = {}
    for e in edges:
        rels[e["rel"]] = rels.get(e["rel"], 0) + 1

    unresolved: list[dict] = []
    if args.sources:
        if len(args.sources) < 2:
            p.error("--from takes the platform AND the artist namespace (pass twice)")
        edges, unresolved = to_cid_space(edges, cid_index(args.fangorn_bin, args.sources))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"generated_from": args.cache_file, "focusArtist": args.focus_artist,
                   "idSpace": "cid" if args.sources else "name",
                   "count": len(edges), "edges": edges}, f)

    print(f"[link] {len(edges)} cross-graph edge(s) → {args.out}")
    for rel, n in sorted(rels.items(), key=lambda kv: -kv[1]):
        print(f"        {rel:<12} {n}")
    if unresolved:
        print(f"[link] ⚠ {len(unresolved)} UNRESOLVED endpoint(s) — these are dead "
              f"ends in the served CDN:", file=sys.stderr)
        for e in unresolved[:10]:
            print(f"        {e['rel']}: {e.get('missingFrom') or ''} "
                  f"{e.get('missingTo') or ''}".rstrip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
