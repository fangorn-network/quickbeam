"""Precompute the UI-shell summaries that the browser currently derives by
scanning every record.

`audius-demo`'s Graph computes three things over the whole corpus:
  buildStats        (graph.ts:248) — the publisher ledger, linkset and convergence
  onboardingOptions (graph.ts:497) — the first-screen genre/artist picker
  sample            (graph.ts:763) — the Home rows, twice per visit to Home

None of them depend on the query, all three are pure functions of the delivered
corpus, and together they are what forces every record to be resident before the
shell can paint. Computing them at bake time and reading them off the manifest lets
the shell render while the shards are still downloading.

THIS IS A SECOND IMPLEMENTATION OF LOGIC THAT ALSO LIVES IN TYPESCRIPT, which is the
same burden `matryoshka` already carries. The JS semantics being mirrored, exactly:
  * `??` is NULLISH — `playCount ?? followerCount` keeps a playCount of 0 and does
    NOT fall through, unlike `||`.
  * `Number(x || 0)` DOES treat 0/''/null alike.
  * Array.prototype.sort is stable (ES2019+), as is Python's sorted, so ties keep
    first-encounter order — provided both sides iterate records in the same order.
`check-graph.ts` asserts the baked values deep-equal the computed ones; that check is
the only thing standing between this file and silent drift.
"""
import glob
import gzip
import json
import os


def load_records(domain_dir: str) -> list[dict]:
    """Read the baked shards into the client's in-memory record shape.

    Mirrors graph.ts:188-206: entityType comes off fields, a non-string owner is
    dropped, and a row without an embedding is skipped (the client cannot index it,
    so it must not appear in a count either)."""
    manifest_path = os.path.join(domain_dir, "manifest.json")
    with open(manifest_path) as f:
        manifest = json.load(f)
    files = [s["file"] for s in manifest.get("shards", [])] or [
        os.path.basename(p) for p in sorted(glob.glob(os.path.join(domain_dir, "shard-*.ndjson.gz")))
    ]
    recs = []
    for name in files:
        path = os.path.join(domain_dir, name)
        if not os.path.exists(path):
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(row.get("embedding"), list):
                    continue
                fields = row.get("fields") or {}
                owner = row.get("owner")
                recs.append({
                    "id": str(row.get("track_id") or ""),
                    "entityType": fields.get("entityType") or "Unknown",
                    "owner": owner if isinstance(owner, str) else None,
                    "fields": fields,
                })
    return recs


def load_edges(domain_dir: str) -> list[dict]:
    path = os.path.join(domain_dir, "edges.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return [e for e in (data.get("edges") or []) if e.get("from") and e.get("to") and e.get("rel")]


def _index(recs):
    return {r["id"]: i for i, r in enumerate(recs)}


def build_stats(recs, edges, platform: str) -> dict:
    """Mirror of graph.ts `buildStats`."""
    platform = (platform or "").lower()
    by_index = _index(recs)

    def owner_of(rid):
        i = by_index.get(rid)
        return recs[i]["owner"] if i is not None else None

    def is_vocab(rid):
        i = by_index.get(rid)
        return i is not None and bool(recs[i]["fields"].get("vocabulary"))

    by_owner: dict[str, dict[str, int]] = {}
    for r in recs:
        o = r["owner"] or "unknown"
        counts = by_owner.setdefault(o, {})
        counts[r["entityType"]] = counts.get(r["entityType"], 0) + 1

    publishers = []
    for owner, counts in by_owner.items():
        own = [r for r in recs
               if r["owner"] == owner and r["entityType"] == "Artist"
               and not r["fields"].get("isReference")]
        p = {"owner": owner, "counts": counts, "total": sum(counts.values())}
        if len(own) == 1:
            # A publisher names itself from its own Artist record — nothing
            # hard-codes which artist the demo is about.
            title = own[0]["fields"].get("title")
            if title is not None:
                p["label"] = title
            p["labelId"] = own[0]["id"]
        publishers.append(p)
    # Platform first, then by size. The TS comparator special-cases the platform and
    # otherwise sorts total descending; this key is equivalent for one platform.
    publishers.sort(key=lambda p: (0 if p["owner"].lower() == platform else 1, -p["total"]))

    linkset_by: dict[str, int] = {}
    for e in edges:
        if is_vocab(e["from"]) or is_vocab(e["to"]):
            continue
        a, b = owner_of(e["from"]), owner_of(e["to"])
        if a and b and a != b:
            linkset_by[e["rel"]] = linkset_by.get(e["rel"], 0) + 1
    linkset = [{"rel": rel, "count": c} for rel, c in linkset_by.items()]
    linkset.sort(key=lambda x: -x["count"])

    vocab_owners: dict[str, set] = {}
    for e in edges:
        if not is_vocab(e["to"]):
            continue
        frm = owner_of(e["from"])
        if not frm:
            continue
        vocab_owners.setdefault(e["to"], set()).add(frm)
    converged = sum(1 for s in vocab_owners.values() if len(s) > 1)

    return {
        "records": len(recs),
        "edges": len(edges),
        "publishers": publishers,
        "linkset": linkset,
        "linksetTotal": sum(x["count"] for x in linkset),
        "converged": converged,
    }


def _adjacency(edges):
    out: dict[str, list] = {}
    inn: dict[str, list] = {}
    for e in edges:
        out.setdefault(e["from"], []).append(e)
        inn.setdefault(e["to"], []).append(e)
    return out, inn


def onboarding_options(recs, edges, genre_n: int = 12, artist_n: int = 12,
                       min_seed_tracks: int = 3) -> dict:
    """Mirror of graph.ts `onboardingOptions` (MIN_SEED_TRACKS = 3, graph.ts:77)."""
    by_index = _index(recs)
    out_adj, in_adj = _adjacency(edges)

    def count(rid, rel, direction):
        es = (out_adj if direction == "out" else in_adj).get(rid, [])
        n = 0
        for e in es:
            if e["rel"] != rel:
                continue
            # An edge whose other endpoint is not a delivered record is not
            # traversable in the client either, so it must not be counted.
            if by_index.get(e["to"] if direction == "out" else e["from"]) is not None:
                n += 1
        return n

    genres = []
    for r in recs:
        if r["entityType"] != "Genre":
            continue
        title = r["fields"].get("title")
        if title is None:
            title = r["fields"].get("id")
        title = "" if title is None else str(title)
        tracks = count(r["id"], "inGenre", "in")
        if tracks > 0 and title:
            genres.append({"id": r["id"], "title": title, "tracks": tracks})
    genres.sort(key=lambda g: -g["tracks"])

    artists = []
    for r in recs:
        if r["entityType"] != "Artist":
            continue
        name = r["fields"].get("artist")
        name = "" if name is None else str(name)
        tracks = count(r["id"], "created", "out")
        followers = r["fields"].get("followerCount") or 0
        if tracks >= min_seed_tracks and name:
            artists.append({"id": r["id"], "name": name, "owner": r["owner"] or "",
                            "tracks": tracks, "followers": int(followers)})
    artists.sort(key=lambda a: -a["followers"])

    return {"genres": genres[:genre_n], "artists": artists[:artist_n]}


def sample(recs, entity_type: str, limit: int, owner: str | None = None) -> list[dict]:
    """Mirror of graph.ts `sample`. `playCount ?? followerCount ?? 0` is NULLISH —
    a playCount of 0 does not fall through to followerCount."""
    want = owner.lower() if owner else None
    pick = [r for r in recs
            if r["entityType"] == entity_type
            and (not want or (r["owner"] or "").lower() == want)]

    def num(r):
        v = r["fields"].get("playCount")
        if v is None:
            v = r["fields"].get("followerCount")
        if v is None:
            v = 0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    pick.sort(key=lambda r: -num(r))
    return [{"id": r["id"], "entityType": r["entityType"], "owner": r["owner"],
             "fields": r["fields"]} for r in pick[:limit]]
