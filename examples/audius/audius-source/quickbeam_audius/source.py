"""The Audius `Source` — two sovereign graphs out of one crawl.

THE DEMO THIS EXISTS FOR
------------------------
Audius' premise is that artists publish their own music. So instead of one graph,
this builds TWO, from a single cache, with the same builder:

  side A ("the platform")  — the trending/underground slice, with the focus artist's
                             OWN CONTENT REMOVED. It keeps a thin `artistref` stub
                             for them, because the platform legitimately knows the
                             artist exists (other artists follow them, playlists
                             reference them) without holding their catalog.
  side B ("the artist")    — the focus artist's full catalog and profile, published
                             from their own wallet to their own namespace.

Edges whose endpoints land on opposite sides are neither side's to publish: they
become the LINKSET (see link.py), which is what fuses the two graphs back into one
searchable whole at read time.

Because both sides come out of the same `build_graph`, they are schema-identical by
construction — a second source package would have guaranteed drift.

A NOTE ON THE SHARED VOCABULARY
-------------------------------
Genre/Mood/Tag nodes are derived on BOTH sides from each side's own tracks. Their
payloads are byte-identical, and Fangorn is content-addressed — so both publishers
independently derive the SAME CID for `Genre: Electronic`. The two graphs converge
at those vertices with no coordination and no linkset entry. That is worth pointing
at during the demo; it is content addressing doing the work a schema registry
usually does.
"""
from __future__ import annotations

import argparse
import json
import os
import re

from .crawl import artist_name, crawl, playlist_name, track_name

# ---------------------------------------------------------------------------
# ROLE MAP + PRESENTATION — drives the `--dry-run` embed preview and rides through
# to the CDN manifest, which is what re-skins the browser app for music.
# ---------------------------------------------------------------------------
AUDIUS_ROLE_MAP: dict = {
    "title":    "title",
    "subtitle": "artist",
    "tags":     ["genre", "mood", "tags"],
    "text":     ["text"],
}

AUDIUS_PRESENTATION: dict = {
    "accent": "#7e1bcc",  # Audius purple
    "icons": {
        "Artist":   "person",
        "Track":    "music_note",
        "Playlist": "queue_music",
        "Genre":    "category",
        "Mood":     "mood",
        "Tag":      "tag",
    },
}

STEMS = {"Artist": "artists", "Track": "tracks", "Playlist": "playlists",
         "Genre": "genres", "Mood": "moods", "Tag": "tags"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")


def genre_name(g: str) -> str: return f"audius:genre:{_slug(g)}"
def mood_name(m: str) -> str:  return f"audius:mood:{_slug(m)}"
def tag_name(t: str) -> str:   return f"audius:tag:{_slug(t)}"
def stub_name(i: str) -> str:  return f"audius:artistref:{i}"


def _split_tags(raw) -> list[str]:
    if not raw:
        return []
    parts = raw if isinstance(raw, list) else str(raw).split(",")
    seen, out = set(), []
    for p in parts:
        s = _slug(str(p))
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _split_genres(raw) -> list[str]:
    """Audius lets a track carry a compound genre ("Tech House, Acid Groove")."""
    if not raw:
        return []
    seen, out = set(), []
    for p in str(raw).split(","):
        g = p.strip()
        if g and g.lower() not in seen:
            seen.add(g.lower())
            out.append(g)
    return out


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clean(s) -> str:
    """Collapse whitespace so a description can't change a node's bytes just by
    re-wrapping — the graph is content-addressed, so formatting is identity."""
    return re.sub(r"\s+", " ", str(s or "")).strip()


# ---------------------------------------------------------------------------
# PURE SHAPERS — one raw object → one node's fields. No IO.
# ---------------------------------------------------------------------------
def track_fields(t: dict) -> dict:
    genres, mood = _split_genres(t.get("genre")), _clean(t.get("mood"))
    tags = _split_tags(t.get("tags"))
    artist = _clean((t.get("user") or {}).get("name") or (t.get("user") or {}).get("handle"))
    desc = _clean(t.get("description"))[:600]
    # The embedded blurb. Search quality lives here, so it verbalizes the facts a
    # listener would actually phrase a query in ("dark acid groove house").
    bits = [f"{_clean(t.get('title'))} by {artist}." if artist else _clean(t.get("title"))]
    if genres:
        bits.append(f"Genre: {', '.join(genres)}.")
    if mood:
        bits.append(f"Mood: {mood}.")
    if tags:
        bits.append(f"Tagged: {', '.join(tags)}.")
    if desc:
        bits.append(desc)
    return {
        "entityType": "Track", "id": t.get("id"), "title": _clean(t.get("title")),
        "artist": artist, "artistId": (t.get("user") or {}).get("id"),
        "artistHandle": (t.get("user") or {}).get("handle"),
        "genre": ", ".join(genres), "mood": mood, "tags": ",".join(tags),
        "description": desc,
        "duration": _num(t.get("duration")), "playCount": _num(t.get("play_count")),
        "favoriteCount": _num(t.get("favorite_count")),
        "repostCount": _num(t.get("repost_count")),
        # Artwork travels as a CID, never a URL: the API hands back a different
        # content-node host on every response, and all of them serve byte-identical
        # bytes for the same CID. Storing a URL would bake in a random mirror.
        # Render as {contentNode}/content/{cid}/480x480.jpg.
        "artworkCid": _clean(t.get("cover_art_sizes")),
        "releaseDate": _clean(t.get("release_date") or t.get("created_at"))[:10],
        "permalink": _clean(t.get("permalink")),
        "isDownloadable": bool(t.get("is_downloadable")),
        "text": " ".join(bits),
    }


def artist_fields(u: dict) -> dict:
    name, handle = _clean(u.get("name")), _clean(u.get("handle"))
    bio, loc = _clean(u.get("bio"))[:600], _clean(u.get("location"))
    bits = [f"{name} (@{handle}) is an artist on Audius." if name else ""]
    if loc:
        bits.append(f"Based in {loc}.")
    if bio:
        bits.append(bio)
    return {
        "entityType": "Artist", "id": u.get("id"), "title": name or handle,
        "artist": handle, "handle": handle, "bio": bio, "location": loc,
        "followerCount": _num(u.get("follower_count")),
        "followeeCount": _num(u.get("followee_count")),
        "trackCount": _num(u.get("track_count")),
        "albumCount": _num(u.get("album_count")),
        "playlistCount": _num(u.get("playlist_count")),
        "isVerified": bool(u.get("is_verified")),
        "wallet": _clean(u.get("erc_wallet") or u.get("wallet")),
        "avatarCid": _clean(u.get("profile_picture_sizes")),
        "bannerCid": _clean(u.get("cover_photo_sizes")),
        "text": " ".join(b for b in bits if b),
    }


def artist_stub_fields(u: dict) -> dict:
    """The platform's REFERENCE to an artist whose catalog it does not hold.

    Deliberately minimal — id, handle, display name. Everything else (bio, counts,
    wallet, catalog) belongs to the artist's own graph. A distinct node name keeps
    it from colliding with the artist's real node when both graphs are embedded into
    one collection.
    """
    name, handle = _clean(u.get("name")), _clean(u.get("handle"))
    return {
        "entityType": "Artist", "id": u.get("id"), "title": name or handle,
        "artist": handle, "handle": handle, "isReference": True,
        "text": f"{name or handle} (@{handle}) — an artist referenced by the platform "
                f"whose catalog is published independently.",
    }


def playlist_fields(p: dict) -> dict:
    name = _clean(p.get("playlist_name"))
    owner = _clean((p.get("user") or {}).get("name") or (p.get("user") or {}).get("handle"))
    desc = _clean(p.get("description"))[:600]
    kind = "Album" if p.get("is_album") else "Playlist"
    bits = [f"{name} — a{'n' if kind == 'Album' else ''} {kind.lower()}"
            + (f" by {owner}." if owner else ".")]
    if desc:
        bits.append(desc)
    return {
        "entityType": "Playlist", "id": p.get("id"), "title": name, "artist": owner,
        "kind": kind, "isAlbum": bool(p.get("is_album")), "description": desc,
        "ownerId": (p.get("user") or {}).get("id"),
        "trackCount": _num(p.get("track_count")),
        "favoriteCount": _num(p.get("favorite_count")),
        "repostCount": _num(p.get("repost_count")),
        "artworkCid": _clean(p.get("cover_art_sizes")),
        "releaseDate": _clean(p.get("created_at"))[:10],
        "permalink": _clean(p.get("permalink")),
        "text": " ".join(bits),
    }


def _vocab_fields(kind: str, label: str, blurb: str) -> dict:
    return {"entityType": kind, "id": _slug(label), "title": label,
            "text": blurb, "vocabulary": True}


# ---------------------------------------------------------------------------
# BUILD_GRAPH — records → (nodes, edges). Pure; the unit-testable core.
# ---------------------------------------------------------------------------
def build_graph(records: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    nodes: dict[str, list[dict]] = {}
    edges: list[dict] = []
    seen_edge: set[tuple] = set()
    present: set[str] = set()

    def put(entity: str, name: str, fields: dict) -> None:
        if name in present:
            return
        present.add(name)
        nodes.setdefault(entity, []).append({"name": name, "fields": fields})

    def link(rel, frm, to, ftype, ttype, **extra):
        key = (rel, frm, to)
        if frm == to or key in seen_edge:
            return
        seen_edge.add(key)
        e = {"rel": rel, "from": frm, "to": to, "fromType": ftype, "toType": ttype}
        if extra:
            e["fields"] = extra
        edges.append(e)

    # -- nodes ---------------------------------------------------------------
    for r in records:
        t, d = r.get("type"), r.get("data") or {}
        if t == "artist":
            put("Artist", artist_name(r["id"]), artist_fields(d))
        elif t == "artist-stub":
            put("Artist", stub_name(r["id"]), artist_stub_fields(d))
        elif t == "track":
            put("Track", track_name(r["id"]), track_fields(d))
        elif t == "playlist":
            put("Playlist", playlist_name(r["id"]), playlist_fields(d))

    # -- derived vocabulary: Genre / Mood / Tag ------------------------------
    # Derived from each side's OWN tracks, so both graphs grow the same vocabulary
    # independently and (being content-addressed) converge on identical CIDs.
    for r in records:
        if r.get("type") != "track":
            continue
        d = r["data"]
        tn, owner = track_name(r["id"]), (d.get("user") or {}).get("id")
        for g in _split_genres(d.get("genre")):
            gn = genre_name(g)
            put("Genre", gn, _vocab_fields("Genre", g, f"{g} — a music genre on Audius."))
            link("inGenre", tn, gn, "Track", "Genre")
            if owner:
                link("worksIn", artist_name(owner), gn, "Artist", "Genre")
        if _clean(d.get("mood")):
            m = _clean(d["mood"])
            mn = mood_name(m)
            put("Mood", mn, _vocab_fields("Mood", m, f"{m} — a mood a track can carry."))
            link("hasMood", tn, mn, "Track", "Mood")
        for tag in _split_tags(d.get("tags")):
            gn = tag_name(tag)
            put("Tag", gn, _vocab_fields("Tag", tag, f"#{tag} — an artist-applied tag."))
            link("taggedWith", tn, gn, "Track", "Tag")

    # -- crawled relations ---------------------------------------------------
    # Dropped if an endpoint isn't in this side's node set: a dangling edge would
    # break the "0 unresolved endpoints" guarantee the CDN join depends on.
    for r in records:
        if r.get("type") != "rel":
            continue
        if r["from"] in present and r["to"] in present:
            link(r["rel"], r["from"], r["to"], r.get("fromType"), r.get("toType"),
                 **(r.get("fields") or {}))

    return nodes, edges


# ---------------------------------------------------------------------------
# PARTITION — one crawl, two sovereign graphs (+ the edges that cross).
# ---------------------------------------------------------------------------
def _owner_of(r: dict) -> str | None:
    return ((r.get("data") or {}).get("user") or {}).get("id")


def node_sides(records: list[dict], focus_id: str) -> dict[str, str]:
    """name → "A" (platform) or "B" (artist). The focus artist's own node is B;
    their content is B; everything else is A."""
    side: dict[str, str] = {}
    for r in records:
        t = r.get("type")
        if t == "artist":
            side[artist_name(r["id"])] = "B" if r["id"] == focus_id else "A"
        elif t == "track":
            side[track_name(r["id"])] = "B" if _owner_of(r) == focus_id else "A"
        elif t == "playlist":
            side[playlist_name(r["id"])] = "B" if _owner_of(r) == focus_id else "A"
    return side


def partition(records: list[dict], focus_id: str, want: str) -> list[dict]:
    """Records for one side.

    Side A additionally gets the artist STUB, and any edge that touched the focus
    artist's *node* is rewritten onto that stub — so A stands alone as a coherent
    graph ("these artists follow someone whose catalog we don't hold") instead of
    dangling. Edges touching the focus artist's *content* genuinely cross and are
    left for the linkset.
    """
    side = node_sides(records, focus_id)
    focus_node = artist_name(focus_id)
    out: list[dict] = []

    for r in records:
        t = r.get("type")
        if t in ("artist", "track", "playlist"):
            nm = {"artist": artist_name, "track": track_name,
                  "playlist": playlist_name}[t](r["id"])
            if side.get(nm) == want:
                out.append(r)
            elif want == "A" and nm == focus_node:
                out.append({**r, "type": "artist-stub"})
        elif t == "rel":
            frm, to = r["from"], r["to"]
            sf, st = side.get(frm), side.get(to)
            if want == "A":
                # Rewrite focus-artist endpoints onto the stub; that edge is then
                # wholly A's to publish.
                f2 = stub_name(focus_id) if frm == focus_node else frm
                t2 = stub_name(focus_id) if to == focus_node else to
                sf2 = "A" if frm == focus_node else sf
                st2 = "A" if to == focus_node else st
                if sf2 == "A" and st2 == "A":
                    out.append({**r, "from": f2, "to": t2})
            elif sf == "B" and st == "B":
                out.append(r)
    return out


def cross_edges(records: list[dict], focus_id: str) -> list[dict]:
    """The linkset: edges neither side can publish alone, plus the `sameAs` that
    identifies the platform's stub with the artist's real node."""
    side = node_sides(records, focus_id)
    focus_node = artist_name(focus_id)
    out = [{"rel": "sameAs", "from": stub_name(focus_id), "to": focus_node,
            "fromType": "Artist", "toType": "Artist"}]
    for r in records:
        if r.get("type") != "rel":
            continue
        frm, to = r["from"], r["to"]
        # An edge onto the focus artist's NODE was absorbed into A via the stub.
        if frm == focus_node or to == focus_node:
            continue
        sf, st = side.get(frm), side.get(to)
        if sf and st and sf != st:
            out.append({k: r[k] for k in ("rel", "from", "to", "fromType", "toType")
                        if r.get(k) is not None})
    return out


# ---------------------------------------------------------------------------
# CACHE — the determinism boundary (see crawl.py's module docstring).
# ---------------------------------------------------------------------------
def load_or_crawl(args) -> list[dict]:
    path = args.cache_file
    if path and os.path.exists(path) and not args.refresh:
        with open(path, encoding="utf-8") as f:
            recs = json.load(f)
        print(f"[audius] cache hit: {len(recs)} record(s) from {path}")
        return recs
    recs = crawl(focus_handle=args.focus_artist, max_artists=args.max_artists,
                 max_trending=args.max_trending, max_playlists=args.max_playlists,
                 focus_playlists=args.focus_playlists,
                 per_artist_tracks=args.per_artist_tracks, workers=args.workers)
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(recs, f)
        print(f"[audius] cached {len(recs)} record(s) → {path}")
    return recs


def focus_id_of(records: list[dict], handle: str) -> str:
    for r in records:
        if r.get("type") == "artist" and \
                ((r.get("data") or {}).get("handle") or "").lower() == handle.lower():
            return r["id"]
    raise SystemExit(f"[audius] focus artist {handle!r} not present in the cache")


# ---------------------------------------------------------------------------
# THE SOURCE
# ---------------------------------------------------------------------------
class AudiusSource:
    """Structurally satisfies `quickbeam.ingest.scrapers.Source` without importing
    it, so this package carries no hard dependency on quickbeam internals."""

    name = "audius"
    stems = STEMS
    # Every entity here is a live snapshot keyed on a stable id (latest wins), not a
    # stream of discrete events — so every stem is rewritten wholesale each cycle.
    snapshot_stems = set(STEMS.values())
    role_map = AUDIUS_ROLE_MAP
    presentation = AUDIUS_PRESENTATION
    default_volume = 1

    def add_source_args(self, p: argparse.ArgumentParser) -> None:
        p.add_argument("--focus-artist", default="disclosure",
                       help="Audius handle of the artist who gets their own sovereign "
                            "graph (default: disclosure — chosen for relational "
                            "richness: 100%% of tracks tagged, 30 with moods, 4 "
                            "genres, remixes, ~2.3k playlist references).")
        p.add_argument("--side", choices=["A", "B", "all"], default="all",
                       help="Which graph to emit: A = the platform slice with the "
                            "focus artist's catalog removed (a stub kept in its "
                            "place); B = the focus artist's own graph; all = "
                            "everything in one graph (no split).")
        p.add_argument("--cache-file", default="./audius_cache.json",
                       help="Raw crawl cache. Present and not --refresh → no network "
                            "at all. This is what makes commits reproducible (and the "
                            "demo immune to a flaky discovery node).")
        p.add_argument("--refresh", action="store_true",
                       help="Re-crawl even if the cache exists (produces a new commit).")
        p.add_argument("--max-artists", type=int, default=150,
                       help="Cap on artists expanded through the 8 relation endpoints.")
        p.add_argument("--max-trending", type=int, default=200,
                       help="Tracks pulled from each of trending and underground.")
        p.add_argument("--max-playlists", type=int, default=60,
                       help="Trending playlists to pull.")
        p.add_argument("--focus-playlists", type=int, default=250,
                       help="Cap on platform playlists fetched because they contain a "
                            "focus-artist track — these become the cross-graph "
                            "`contains` edges, the most legible part of the demo. "
                            "Only ~1 in 3 still resolves (the rest are deleted and "
                            "404 on both the v1 and full APIs), so this is set well "
                            "above the number of edges you actually want.")
        p.add_argument("--per-artist-tracks", type=int, default=30,
                       help="Tracks per non-focus artist (the focus artist is always "
                            "pulled in full).")
        p.add_argument("--workers", type=int, default=4,
                       help="Parallel API requests. Audius now serves from one "
                            "rate-limited endpoint, so this stays low; the client "
                            "also self-throttles on 429. Results are re-sorted, so "
                            "this never affects the output bytes.")

    def read(self, cursor: int, args: argparse.Namespace) -> list[dict]:
        """Load (or build) the cache, then hand back only this side's records — so
        `build_graph` stays a pure function of records with no notion of sides."""
        recs = load_or_crawl(args)
        if args.side == "all":
            return recs
        fid = focus_id_of(recs, args.focus_artist)
        part = partition(recs, fid, args.side)
        print(f"[audius] side {args.side}: {len(part)}/{len(recs)} record(s) "
              f"(focus {args.focus_artist} = {fid})")
        return part

    def build_graph(self, records: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
        return build_graph(records)

    def next_cursor(self, records: list[dict], prev: int) -> int:
        """A snapshot source: the cache, not a block height, defines the state.
        Nothing advances a cursor, so the harness's watch/checkpoint machinery
        no-ops harmlessly."""
        return prev


def run() -> None:
    from quickbeam.ingest.scrapers.harness import run_source
    run_source(AudiusSource())


if __name__ == "__main__":
    run()
