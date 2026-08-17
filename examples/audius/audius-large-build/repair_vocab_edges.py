"""Backfill the vocabulary edges that dump_source.py dropped.

WHAT HAPPENED. `link()` refuses any edge whose endpoints are not yet in the `side`
map, and vocabulary nodes were registered there only AFTER the track loop that emits
their edges. So every inGenre / hasMood / taggedWith / worksIn edge was discarded
without a word: the mesh came out with 12 relation types instead of 16, `converged`
would have been 0, and — because `onboardingOptions` needs Genre nodes with inbound
`inGenre` edges — the app would have been stuck on the onboarding screen forever.

WHY THIS DOES NOT NEED A RE-EMBED. The vocabulary NODES were emitted correctly and are
already in Qdrant and in the baked shards. Only the edges are missing, and they are
pure derivations of `fields.genre/mood/tags` on tracks that are already staged. So
this re-derives them from disk in minutes rather than repeating a 7-hour embed.

The derivation imports the same helpers dump_source uses, so the edges are identical
to what a corrected full run would have produced.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "audius-source"))
from quickbeam_audius.crawl import artist_name, track_name  # noqa: E402
from quickbeam_audius.source import (  # noqa: E402
    _clean, _split_genres, _split_tags, genre_name, mood_name, tag_name,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage"))
    args = ap.parse_args()

    db = sqlite3.connect(os.path.join(args.stage, "edges.sqlite"))
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
    before = db.execute("SELECT count(*) FROM edge").fetchone()[0]
    have = {r[0] for r in db.execute("SELECT DISTINCT rel FROM edge")}
    print(f"[repair] {before} edges, {len(have)} relations: {sorted(have)}")

    seen: set[tuple] = set()
    per_side: dict[int, list] = {1: [], 2: []}
    buf: list[tuple] = []

    def add(vol, rel, src, dst, st, dt):
        key = (rel, src, dst)
        if src == dst or key in seen:
            return
        seen.add(key)
        buf.append((rel, src, dst, st, dt))
        # Vocabulary edges are always INTRA-side: both publishers derive their own copy
        # of a shared Genre/Mood/Tag, so a side-B track's genre edge belongs to side B,
        # not to the linkset.
        per_side[vol].append({"rel": rel, "from": src, "to": dst,
                              "fromType": st, "toType": dt})

    for vol in (1, 2):
        path = os.path.join(args.stage, f"volume_{vol}_tracks.json")
        if not os.path.exists(path):
            continue
        rows = json.load(open(path, encoding="utf-8"))
        print(f"[repair] volume {vol}: {len(rows)} tracks", flush=True)
        for r in rows:
            f = r["fields"]
            tn = r["name"]
            artist = artist_name(f["artistId"]) if f.get("artistId") else None
            for g in _split_genres(f.get("genre")):
                gn = genre_name(g)
                add(vol, "inGenre", tn, gn, "Track", "Genre")
                if artist:
                    add(vol, "worksIn", artist, gn, "Artist", "Genre")
            m = _clean(f.get("mood"))
            if m:
                add(vol, "hasMood", tn, mood_name(m), "Track", "Mood")
            for tg in _split_tags(f.get("tags")):
                add(vol, "taggedWith", tn, tag_name(tg), "Track", "Tag")
            if len(buf) >= 200_000:
                db.executemany("INSERT INTO edge VALUES (?,?,?,?,?)", buf)
                buf.clear()
        del rows

    if buf:
        db.executemany("INSERT INTO edge VALUES (?,?,?,?,?)", buf)
    db.commit()

    # Append to the per-side edge files. They are JSON arrays written by ArrayWriter,
    # so splice before the closing bracket rather than parsing 2.1 GB back in.
    for vol in (1, 2):
        if not per_side[vol]:
            continue
        path = os.path.join(args.stage, f"volume_{vol}_edges.json")
        with open(path, "r+", encoding="utf-8") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(0, end - 1))
            tail = fh.read()
            fh.seek(max(0, end - 1))
            if tail.strip() != "]":
                print(f"[repair] WARNING: {path} does not end with ']' — skipping splice")
                continue
            body = ",".join(json.dumps(e, separators=(",", ":")) for e in per_side[vol])
            # An empty array is "[]": no leading comma in that case.
            fh.write(("" if end <= 2 else ",") + body + "]")
        print(f"[repair] volume_{vol}_edges.json += {len(per_side[vol])}")

    after = db.execute("SELECT count(*) FROM edge").fetchone()[0]
    rels = {r[0]: r[1] for r in db.execute("SELECT rel,count(*) FROM edge GROUP BY rel")}
    db.close()

    print(f"[repair] {before} -> {after} edges (+{after - before})")
    for r in sorted(rels, key=lambda k: -rels[k]):
        print(f"   {r:12s} {rels[r]:>10,}")

    want = {"contains", "created", "curated", "favorited", "follows", "hasMood",
            "hasStem", "inGenre", "relatedTo", "released", "remixOf", "reposted",
            "sameAs", "supports", "taggedWith", "worksIn"}
    missing = sorted(want - set(rels))
    assert not missing, f"still missing relations: {missing}"
    assert rels.get("inGenre", 0) > 0, "inGenre empty — onboarding would be unreachable"
    print("[repair] OK — all 16 relations present")


if __name__ == "__main__":
    main()
