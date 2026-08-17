"""Cut the `audius-home` bootstrap domain out of the baked `audius-large` domain.

This is what the browser downloads whole at page load. It must be small AND a complete
graph on its own: the home page, the onboarding picker and every relation rail render
from it with ZERO query disclosure, before any search touches the network.

It filters the BAKED shards rather than re-staging for `prebake`, because those rows
already carry their embeddings — the 7-hour embed is not repeated, and a record that
appears in both domains is guaranteed the same id, fields and vector, which is what
lets a search hit dedupe against the resident bootstrap instead of appearing twice.

Selection:
  * every track the focus artist published (side B) — the sovereignty story has to be
    present at load, not only after you search
  * the top --tracks side-A tracks by play count
  * the artists those tracks reference
  * Genre and Mood in full (644 nodes, and onboarding depends on Genre)
  * the top --tags tags by usage among the selected tracks. NOT all 210,581: at
    ~2.6 KB/record the full vocabulary would be ~550 MB of a bootstrap meant to be
    ~100 MB, and the long tail is tags used by one track each.
  * every edge whose BOTH endpoints survived the cut

HARD REQUIREMENT. `Onboarding.tsx:38` gates the whole app behind `>=3 genres and
exactly 3 artists`, sourced from `onboardingOptions`, which needs Genre nodes with
inbound `inGenre` edges and Artists with >= 3 outbound `created` edges. A bootstrap
that fails that is an app with no way in — there is no skip button. Asserted here, at
build time. (The vocabulary-edge bug that produced zero `inGenre` edges is exactly the
class of failure these assertions exist to stop.)
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter, defaultdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--src-domain", required=True, help="Baked audius-large domain dir.")
    ap.add_argument("--out-domain", required=True, help="Output audius-home domain dir.")
    ap.add_argument("--tracks", type=int, default=25_000)
    ap.add_argument("--tags", type=int, default=5_000)
    ap.add_argument("--shard-size", type=int, default=5_000)
    ap.add_argument("--playlists", type=int, default=3_000)
    ap.add_argument("--edge-cap", type=int, default=60,
                    help="Max edges per (endpoint, relation, direction).")
    args = ap.parse_args()

    # ---- choose the nodes ----------------------------------------------------
    keep: set[str] = set()
    side_b = json.load(open(os.path.join(args.stage, "volume_2_tracks.json"), encoding="utf-8"))
    for r in side_b:
        keep.add(r["name"])
    print(f"[bootstrap] side B tracks: {len(side_b)}")

    top = json.load(open(os.path.join(args.stage, "volume_1_tracks.json"), encoding="utf-8"))
    top.sort(key=lambda r: -(r["fields"].get("playCount") or 0))
    top = top[:args.tracks]
    tag_use: Counter = Counter()
    artists: set[str] = set()
    for r in top:
        keep.add(r["name"])
        if r["fields"].get("artistId"):
            artists.add(f"audius:user:{r['fields']['artistId']}")
        for t in str(r["fields"].get("tags") or "").split(","):
            if t:
                tag_use[f"audius:tag:{t}"] += 1
    for r in side_b:
        if r["fields"].get("artistId"):
            artists.add(f"audius:user:{r['fields']['artistId']}")
        for t in str(r["fields"].get("tags") or "").split(","):
            if t:
                tag_use[f"audius:tag:{t}"] += 1
    keep |= artists
    for name, _ in tag_use.most_common(args.tags):
        keep.add(name)
    del top, side_b

    for stem in ("genres", "moods"):
        for r in json.load(open(os.path.join(args.stage, f"volume_1_{stem}.json"), encoding="utf-8")):
            keep.add(r["name"])

    # The platform's REFERENCE to the focus artist. Without it there is no `sameAs`
    # and no visible hole where the artist's catalogue would be — which is the entire
    # sovereignty story the home page tells. It lives on side A as a separate node
    # from the artist's real one, so selecting by artistId never picks it up.
    for r in json.load(open(os.path.join(args.stage, "volume_1_artists.json"), encoding="utf-8")):
        if r["fields"].get("isReference"):
            keep.add(r["name"])
            print(f"[bootstrap] artist stub: {r['name']}")

    # Playlists that hold a selected track, so `contains` / `curated` / `released`
    # survive and entity pages have their "Appears in" rails. Without any Playlist
    # nodes those three relations vanish from the bootstrap entirely.
    db0 = sqlite3.connect(os.path.join(args.stage, "edges.sqlite"))
    pl: Counter = Counter()
    for src, dst in db0.execute("SELECT src,dst FROM edge WHERE rel='contains'"):
        if dst in keep:
            pl[src] += 1
    db0.close()
    for name, _ in pl.most_common(args.playlists):
        keep.add(name)
    print(f"[bootstrap] playlists holding a selected track: {len(pl)}, "
          f"keeping {min(args.playlists, len(pl))}")
    print(f"[bootstrap] selected {len(keep)} nodes "
          f"({len(artists)} artists, {min(args.tags, len(tag_use))} tags)")

    # ---- filter the baked shards --------------------------------------------
    tmp = args.out_domain + ".tmp"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    src_manifest = json.load(open(os.path.join(args.src_domain, "manifest.json")))

    shards, idx, in_shard, fh, total = [], -1, 0, None, 0
    written: set[str] = set()
    owner_of: dict[str, str] = {}

    def close_shard():
        nonlocal fh
        if fh is None:
            return
        fh.close()
        p = os.path.join(tmp, f"shard-{idx:04d}.ndjson.gz")
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        name = f"shard-{idx:04d}-{h[:12]}.ndjson.gz"
        os.replace(p, os.path.join(tmp, name))
        shards.append({"file": name, "count": in_shard,
                       "bytes": os.path.getsize(os.path.join(tmp, name)), "sha256": h})

    def open_shard():
        nonlocal fh, idx, in_shard
        idx += 1
        in_shard = 0
        fh = gzip.GzipFile(os.path.join(tmp, f"shard-{idx:04d}.ndjson.gz"), mode="wb", mtime=0)

    # Counts of the FULL corpus, tallied on the same pass that filters it.
    # The client needs both numbers and they are not interchangeable: `stats` describes
    # what is RESIDENT (41k records — what rails and onboarding actually traverse),
    # while this describes what is SEARCHABLE. Showing the resident figure where the UI
    # makes its scale claim understates the catalogue by ~57x, which is the opposite of
    # the pitch.
    cat_counts: dict[str, Counter] = {}
    cat_total = 0

    open_shard()
    for s in src_manifest["shards"]:
        with gzip.open(os.path.join(args.src_domain, s["file"]), "rt", encoding="utf-8") as src:
            for line in src:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                o = row.get("owner") or "unknown"
                et = (row.get("fields") or {}).get("entityType") or "Unknown"
                cat_counts.setdefault(o, Counter())[et] += 1
                cat_total += 1
                if row.get("track_id") not in keep:
                    continue
                if in_shard >= args.shard_size:
                    close_shard()
                    open_shard()
                fh.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
                written.add(row["track_id"])
                if row.get("owner"):
                    owner_of[row["track_id"]] = row["owner"]
                in_shard += 1
                total += 1
    close_shard()

    # Edges are filtered against what was ACTUALLY WRITTEN, not what we meant to keep.
    # The two differ: an artist referenced by a selected track's `artistId` can fail
    # the content filter upstream and never get a node, and the resulting edge would
    # dangle. A dangling endpoint is a rail that renders empty in the UI, so the
    # "0 unresolved endpoints" invariant has to be enforced against reality.
    missing = len(keep) - len(written)
    if missing:
        print(f"[bootstrap] {missing} selected names had no baked record — "
              f"their edges are dropped rather than left dangling")
    keep = written

    manifest = {k: src_manifest[k] for k in src_manifest
                if k not in ("shards", "count", "stats", "onboarding", "samples")}
    manifest["count"] = total
    manifest["shards"] = shards
    # What the whole catalogue holds, as opposed to what was downloaded. `cdn
    # precompute` preserves unknown manifest keys, so this survives it.
    manifest["catalogue"] = {
        "records": cat_total,
        "publishers": sorted(
            ({"owner": o, "total": sum(c.values()), "counts": dict(c)}
             for o, c in cat_counts.items()),
            key=lambda p: -p["total"]),
    }
    print(f"[bootstrap] catalogue: {cat_total} records across {len(cat_counts)} publishers")
    json.dump(manifest, open(os.path.join(tmp, "manifest.json"), "w"))
    print(f"[bootstrap] {total} records in {len(shards)} shards")

    # ---- edges among the survivors ------------------------------------------
    db = sqlite3.connect(os.path.join(args.stage, "edges.sqlite"))
    edges = []
    ingenre, created = defaultdict(int), defaultdict(int)
    # Cap per (endpoint, relation, direction). Uncapped this is 1.8M edges / 216 MB —
    # dominated by `follows` and `favorited` among 7,888 artists — for a bootstrap
    # whose job is FIRST PAINT. A rail shows 12 (60 for a page's primary relation) and
    # "Show all" goes to the adjacency service, which holds the complete 25.9M mesh.
    # Capping both directions keeps a hub node from monopolising the budget.
    # Cap SOCIAL relations only. They are what explodes — follows 753k + favorited
    # 459k + reposted 334k was 1.55M of the 1.8M — while structural relations are
    # bounded by the data itself. Capping structural ones truncates real catalogues:
    # at cap=60 the focus artist's 71 tracks became 60 `created` edges, which breaks
    # the "this artist's complete catalogue" claim the whole sovereignty story rests on.
    SOCIAL = {"follows", "favorited", "reposted", "relatedTo", "supports"}
    cap = args.edge_cap
    out_n, in_n = Counter(), Counter()
    for rel, src, dst, st, dt in db.execute("SELECT rel,src,dst,stype,dtype FROM edge"):
        if src not in keep or dst not in keep:
            continue
        # Counts are taken BEFORE the cap: onboarding ranks genres and artists by how
        # many tracks they hold, and a capped count would reorder the picker.
        if rel == "inGenre":
            ingenre[dst] += 1
        elif rel == "created":
            created[src] += 1
        if rel in SOCIAL:
            ok, ik = (src, rel), (dst, rel)
            if out_n[ok] >= cap or in_n[ik] >= cap:
                continue
            out_n[ok] += 1
            in_n[ik] += 1
        edges.append({"rel": rel, "from": src, "to": dst, "fromType": st, "toType": dt})
    db.close()
    json.dump({"generated_at": 0, "count": len(edges),
               "relations": sorted({e["rel"] for e in edges}), "edges": edges},
              open(os.path.join(tmp, "edges.json"), "w"))
    with open(os.path.join(tmp, "edges.json"), "rb") as fin, \
            gzip.GzipFile(os.path.join(tmp, "edges.json.gz"), mode="wb", mtime=0) as fout:
        shutil.copyfileobj(fin, fout)
    cross = [e for e in edges
             if not e["from"].startswith(("audius:genre:", "audius:mood:", "audius:tag:"))
             and not e["to"].startswith(("audius:genre:", "audius:mood:", "audius:tag:"))
             and owner_of.get(e["from"]) and owner_of.get(e["to"])
             and owner_of[e["from"]] != owner_of[e["to"]]]
    json.dump({"count": len(cross), "edges": cross},
              open(os.path.join(tmp, "linkset.json"), "w"))
    print(f"[bootstrap] {len(edges)} edges, "
          f"{len(sorted({e['rel'] for e in edges}))} relations, linkset {len(cross)}")

    if os.path.exists(args.out_domain):
        shutil.rmtree(args.out_domain)
    os.replace(tmp, args.out_domain)

    # ---- the assertions that keep the app reachable -------------------------
    genres_ok = sum(1 for c in ingenre.values() if c > 0)
    artists_ok = sum(1 for c in created.values() if c >= 3)
    print(f"[bootstrap] onboarding: {genres_ok} genres with tracks, "
          f"{artists_ok} artists with >=3 tracks")
    assert genres_ok >= 3, "onboarding unreachable: needs >=3 genres with inGenre edges"
    assert artists_ok >= 3, "onboarding unreachable: needs >=3 artists with >=3 created edges"
    print("[bootstrap] OK")


if __name__ == "__main__":
    main()
