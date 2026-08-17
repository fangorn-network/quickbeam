"""Audius pg_dump -> the staged graph, at full scale.

Replaces the API crawl as the INPUT to the existing model. The node shapers in
`audius-source/quickbeam_audius/source.py` are imported and reused verbatim, so node
bytes stay identical to the crawl's and the demo UI needs no change.

WHY SQL. The records carry numbers the dump does not pre-materialize: play counts are
in `aggregate_plays`, save/repost counts in `aggregate_track`, follower/track counts in
`aggregate_user`, and `permalink` is (users.handle, track_routes.slug). Reading them
off `tracks`/`users` yields zeros, which silently destroys every popularity ordering —
including the bootstrap selection, which is chosen by play count.

WHY THIS STREAMS INSTEAD OF CALLING `build_graph`. `build_graph` accumulates every
node AND edge in memory. The mesh here is ~52M edges (follows alone is 26.1M), so
edges go straight to SQLite as they are produced and nodes stream to their volume
files. The only large resident structure is the node->side map (~1.9M entries), which
edge routing genuinely needs.

Outputs, under --out-dir:
    volume_1_*.json   side A: the platform, everything except the focus artist
    volume_2_*.json   side B: the focus artist's own catalogue
    linkset.json      edges neither side can publish alone
    edges.sqlite      EVERY edge, for the adjacency service
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "audius-source"))
from quickbeam_audius.crawl import (  # noqa: E402
    artist_name, encode_id, playlist_name, track_name,
)
from quickbeam_audius.source import (  # noqa: E402
    STEMS, _split_genres, _split_tags, _vocab_fields, artist_fields,
    artist_stub_fields, genre_name, mood_name, playlist_fields, stub_name,
    tag_name, track_fields, _clean,
)

DSN = os.environ.get("AUDIUS_PG", "postgresql://postgres:audius@localhost:55432/audius")
TRACK_WHERE = ("t.is_current AND NOT t.is_delete AND NOT t.is_unlisted "
               "AND t.is_available AND coalesce(btrim(t.title),'') <> ''")
PLAYLIST_WHERE = ("p.is_current AND NOT p.is_delete AND NOT p.is_private "
                  "AND coalesce(btrim(p.playlist_name),'') <> ''")
USER_WHERE = ("u.is_current AND NOT u.is_deactivated AND coalesce(a.track_count,0)"
              "+coalesce(a.playlist_count,0)+coalesce(a.album_count,0) > 0")


_VOCAB_PREFIXES = ("audius:genre:", "audius:mood:", "audius:tag:")


def _is_vocab(name: str) -> bool:
    """Vocabulary nodes are the ones both publishers derive independently."""
    return name.startswith(_VOCAB_PREFIXES)


class ArrayWriter:
    """Stream a JSON array without holding it. `harness.emit_volumes` builds the whole
    list first, which does not survive 1.4M records."""

    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.f.write("[")
        self.n = 0

    def add(self, obj):
        if self.n:
            self.f.write(",")
        json.dump(obj, self.f, separators=(",", ":"), ensure_ascii=False)
        self.n += 1

    def close(self):
        self.f.write("]")
        self.f.close()
        return self.n


class Edges:
    """Every edge, once, plus the per-side routing. Indexes are built after the bulk
    insert — creating them first turns minutes into hours at 52M rows."""

    def __init__(self, path, out_dir):
        for p in (path, path + "-journal"):
            if os.path.exists(p):
                os.remove(p)
        self.db = sqlite3.connect(path)
        self.db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                              "CREATE TABLE edge(rel TEXT,src TEXT,dst TEXT,"
                              "stype TEXT,dtype TEXT);")
        self.buf = []
        self.n = 0
        self.side_w = {"A": ArrayWriter(os.path.join(out_dir, "volume_1_edges.json")),
                       "B": ArrayWriter(os.path.join(out_dir, "volume_2_edges.json"))}
        self.link = ArrayWriter(os.path.join(out_dir, "linkset_edges.json"))
        self.counts = {"A": 0, "B": 0, "link": 0}
        self.seen = set()

    def add(self, rel, src, dst, stype, dtype, side_of):
        if src == dst:
            return
        key = (rel, src, dst)
        if key in self.seen:
            return
        self.seen.add(key)
        self.buf.append((rel, src, dst, stype, dtype))
        self.n += 1
        if len(self.buf) >= 200_000:
            self._flush()
        e = {"rel": rel, "from": src, "to": dst, "fromType": stype, "toType": dtype}
        sa, sb = side_of.get(src), side_of.get(dst)
        # A vocabulary endpoint does not make an edge cross-publisher. BOTH sides
        # derive their own Genre/Mood/Tag from their own tracks and converge on one
        # content address — that convergence is the demo's point. The global side map
        # can only record one owner for the shared node, so routing a side-B track's
        # inGenre edge by the vocab node's side would file it in the linkset. That is
        # precisely the mistake that once reported 12,657 crossing edges against a
        # real linkset of 113, and buildStats guards against it the same way
        # (graph.ts isVocab). Route by the source instead.
        if sa and _is_vocab(dst):
            self.side_w[sa].add(e)
            self.counts[sa] += 1
        elif sa and sa == sb:
            self.side_w[sa].add(e)
            self.counts[sa] += 1
        elif sa and sb:
            self.link.add(e)
            self.counts["link"] += 1

    def _flush(self):
        if self.buf:
            self.db.executemany("INSERT INTO edge VALUES (?,?,?,?,?)", self.buf)
            self.buf.clear()

    def finish(self):
        self._flush()
        self.db.commit()
        print(f"[dump] indexing {self.n} edges", flush=True)
        self.db.executescript("CREATE INDEX edge_src ON edge(src);"
                              "CREATE INDEX edge_dst ON edge(dst);")
        self.db.commit()
        self.db.close()
        for w in self.side_w.values():
            w.close()
        self.link.close()


def cursor(conn, name):
    c = conn.cursor(name=name)
    c.itersize = 20_000
    return c


def main():
    ap = argparse.ArgumentParser(prog="dump_source")
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage"))
    ap.add_argument("--focus-handle", default="disclosure")
    ap.add_argument("--limit", type=int, default=0, help="Cap tracks (dev runs).")
    args = ap.parse_args()

    # fields.id is what player.tsx builds the stream URL from, so a wrong encoding is
    # silent: metadata and artwork render, only playback fails.
    assert encode_id(1192383172) == "AlQM0Bg", f"hashid drift: {encode_id(1192383172)}"
    print("[dump] hashid self-check ok", flush=True)
    os.makedirs(args.out_dir, exist_ok=True)
    # ORDER BY is not cosmetic: LIMIT without it may return a DIFFERENT set on each
    # execution, so the side map (pass 1) and the node pass (pass 3) would disagree
    # and every put() would silently drop. Full runs have no LIMIT and are unaffected,
    # which is exactly why this only ever bites a dev run.
    lim = f"ORDER BY t.track_id LIMIT {args.limit}" if args.limit else ""

    conn = psycopg.connect(DSN)
    with conn.cursor() as c:
        c.execute("SELECT user_id FROM users WHERE handle=%s AND is_current LIMIT 1",
                  (args.focus_handle,))
        row = c.fetchone()
        if not row:
            sys.exit(f"[dump] focus artist {args.focus_handle!r} not found")
        focus = encode_id(row[0])
    print(f"[dump] focus artist {args.focus_handle} = {focus}", flush=True)

    # ---- pass 1: node -> side. Edge routing needs this for every endpoint. -------
    side: dict[str, str] = {}
    with cursor(conn, "s_tracks") as c:
        c.execute(f"SELECT t.track_id, t.owner_id FROM tracks t WHERE {TRACK_WHERE} {lim}")
        for tid, owner in c:
            h = encode_id(tid)
            if h:
                side[track_name(h)] = "B" if encode_id(owner) == focus else "A"
    with cursor(conn, "s_users") as c:
        c.execute(f"SELECT u.user_id FROM users u JOIN aggregate_user a USING(user_id) "
                  f"WHERE {USER_WHERE}")
        for (uid,) in c:
            h = encode_id(uid)
            if h:
                side[artist_name(h)] = "B" if h == focus else "A"
    with cursor(conn, "s_pl") as c:
        c.execute(f"SELECT p.playlist_id, p.playlist_owner_id FROM playlists p WHERE {PLAYLIST_WHERE}")
        for pid, owner in c:
            h = encode_id(pid)
            if h:
                side[playlist_name(h)] = "B" if encode_id(owner) == focus else "A"
    # Side A references the focus artist through a stub instead of holding their node.
    side[stub_name(focus)] = "A"
    print(f"[dump] {len(side)} nodes sided", flush=True)

    writers: dict[tuple, ArrayWriter] = {}

    def w(vol, stem):
        k = (vol, stem)
        if k not in writers:
            writers[k] = ArrayWriter(os.path.join(args.out_dir, f"volume_{vol}_{stem}.json"))
        return writers[k]

    def put(name, entity, fields):
        s = side.get(name)
        if s:
            w(1 if s == "A" else 2, STEMS[entity]).add({"name": name, "fields": fields})

    edges = Edges(os.path.join(args.out_dir, "edges.sqlite"), args.out_dir)

    def link(rel, src, dst, st, dt):
        if src in side and dst in side:
            edges.add(rel, src, dst, st, dt, side)

    def vocab_node(name, entity, fields):
        """Register a vocabulary node the moment it is first seen.

        THIS ORDERING IS THE WHOLE POINT. `link()` drops any edge whose endpoints are
        not yet in `side`, and vocabulary nodes used to be registered only after the
        track loop finished — so every inGenre/hasMood/taggedWith/worksIn edge was
        silently discarded. Nothing errored; the mesh just came out with 12 relation
        types instead of 16, and because onboardingOptions needs Genre nodes with
        inbound inGenre edges, the app would have been permanently stuck on the
        onboarding screen with no way past it.

        Vocabulary is sided "A" only so the node has a home for routing; the edges
        into it are routed by their SOURCE's side (see Edges.add), because both
        publishers derive their own copy of a shared Genre/Mood/Tag — that
        independent derivation converging on one content address is the demo's point,
        and treating a side-B track's genre edge as cross-publisher is exactly what
        once inflated the linkset from 113 to 12,657.
        """
        if name not in vocab:
            vocab[name] = (entity, fields)
    
    # ---- pass 2: artists --------------------------------------------------------
    with cursor(conn, "artists") as c:
        c.execute(f"""SELECT u.user_id,u.handle,u.name,u.bio,u.location,u.is_verified,
                             u.wallet,u.profile_picture_sizes,u.cover_photo_sizes,
                             a.follower_count,a.following_count,a.track_count,
                             a.album_count,a.playlist_count
                      FROM users u JOIN aggregate_user a USING(user_id) WHERE {USER_WHERE}""")
        n = 0
        for (uid, handle, name, bio, loc, ver, wal, pfp, ban, fo, fi, tc, ac, pc) in c:
            h = encode_id(uid)
            if not h:
                continue
            d = {"id": h, "handle": handle or "", "name": name or "", "bio": bio or "",
                 "location": loc or "", "is_verified": bool(ver), "wallet": wal or "",
                 "profile_picture_sizes": pfp or "", "cover_photo_sizes": ban or "",
                 "follower_count": fo or 0, "followee_count": fi or 0,
                 "track_count": tc or 0, "album_count": ac or 0, "playlist_count": pc or 0}
            if h == focus:
                # A holds the stub, B holds the real node — the whole sovereignty split.
                w(1, "artists").add({"name": stub_name(h), "fields": artist_stub_fields(d)})
                w(2, "artists").add({"name": artist_name(h), "fields": artist_fields(d)})
                edges.add("sameAs", stub_name(h), artist_name(h), "Artist", "Artist", side)
            else:
                put(artist_name(h), "Artist", artist_fields(d))
            n += 1
    print(f"[dump] artists {n}", flush=True)

    # ---- pass 3: tracks (+ vocabulary, + created/inGenre/hasMood/taggedWith) -----
    vocab: dict[str, tuple] = {}   # node name -> (entity, fields)
    with cursor(conn, "tracks") as c:
        c.execute(f"""SELECT t.track_id,t.title,t.genre,t.mood,t.tags,t.description,
                             t.duration,t.cover_art_sizes,t.release_date,t.created_at,
                             t.is_downloadable,t.owner_id,u.handle,u.name,
                             coalesce(ap.count,0),coalesce(ag.save_count,0),
                             coalesce(ag.repost_count,0),r.slug,t.remix_of
                      FROM tracks t
                      JOIN users u ON u.user_id=t.owner_id AND u.is_current
                      LEFT JOIN aggregate_plays ap ON ap.play_item_id=t.track_id
                      LEFT JOIN aggregate_track ag ON ag.track_id=t.track_id
                      LEFT JOIN track_routes r ON r.track_id=t.track_id AND r.is_current
                      WHERE {TRACK_WHERE} {lim}""")
        n = 0
        for (tid, title, genre, mood, tags, desc, dur, art, rel, cre, dl, owner,
             oh, on_, plays, saves, reposts, slug, remix) in c:
            h, ohash = encode_id(tid), encode_id(owner)
            if not h or not ohash:
                continue
            d = {"id": h, "title": title or "", "genre": genre or "", "mood": mood or "",
                 "tags": tags or "", "description": desc or "", "duration": dur or 0,
                 "cover_art_sizes": art or "", "release_date": str(rel or cre or "")[:10],
                 "created_at": str(cre or "")[:10], "is_downloadable": bool(dl),
                 "play_count": plays, "favorite_count": saves, "repost_count": reposts,
                 "permalink": f"/{oh}/{slug}" if oh and slug else "",
                 "user": {"id": ohash, "handle": oh or "", "name": on_ or ""}}
            tn = track_name(h)
            put(tn, "Track", track_fields(d))
            link("created", artist_name(ohash), tn, "Artist", "Track")
            for g in _split_genres(genre):
                gn = genre_name(g)
                vocab_node(gn, "Genre", _vocab_fields("Genre", g, f"{g} — a music genre on Audius."))
                link("inGenre", tn, gn, "Track", "Genre")
                # worksIn has no presence guard upstream; only emit when the owner is a node.
                if artist_name(ohash) in side:
                    link("worksIn", artist_name(ohash), gn, "Artist", "Genre")
            m = _clean(mood)
            if m:
                mn = mood_name(m)
                vocab_node(mn, "Mood", _vocab_fields("Mood", m, f"{m} — a mood a track can carry."))
                link("hasMood", tn, mn, "Track", "Mood")
            for tg in _split_tags(tags):
                gn = tag_name(tg)
                vocab_node(gn, "Tag", _vocab_fields("Tag", tg, f"#{tg} — an artist-applied tag."))
                link("taggedWith", tn, gn, "Track", "Tag")
            if remix:
                try:
                    parents = (remix if isinstance(remix, dict) else json.loads(remix)).get("tracks") or []
                    for p in parents:
                        ph = encode_id(p.get("parent_track_id"))
                        if ph:
                            link("remixOf", tn, track_name(ph), "Track", "Track")
                except Exception:  # noqa: BLE001 - a malformed remix_of must not stop the build
                    pass
            n += 1
            if n % 250_000 == 0:
                print(f"[dump] tracks {n}", flush=True)
    print(f"[dump] tracks {n}, vocabulary {len(vocab)}", flush=True)

    # Vocabulary is derived per side, which is what makes both graphs converge on the
    # same content address for a shared Genre/Mood/Tag without any coordination.
    for name, (entity, fields) in vocab.items():
        for vol in (1, 2):
            w(vol, STEMS[entity]).add({"name": name, "fields": fields})
    print(f"[dump] vocabulary emitted to both sides", flush=True)

    # ---- pass 4: playlists + contains ------------------------------------------
    with cursor(conn, "pl") as c:
        c.execute(f"""SELECT p.playlist_id,p.playlist_name,p.description,p.is_album,
                             p.playlist_owner_id,p.created_at,p.playlist_image_sizes_multihash,
                             u.handle,u.name,coalesce(ag.save_count,0),coalesce(ag.repost_count,0),
                             (SELECT count(*) FROM playlist_tracks pt
                                WHERE pt.playlist_id=p.playlist_id AND NOT pt.is_removed)
                      FROM playlists p
                      JOIN users u ON u.user_id=p.playlist_owner_id AND u.is_current
                      LEFT JOIN aggregate_playlist ag ON ag.playlist_id=p.playlist_id
                      WHERE {PLAYLIST_WHERE}""")
        n = 0
        for (pid, name, desc, alb, owner, cre, art, oh, on_, saves, reposts, tc) in c:
            h, ohash = encode_id(pid), encode_id(owner)
            if not h or not ohash:
                continue
            d = {"id": h, "playlist_name": name or "", "description": desc or "",
                 "is_album": bool(alb), "created_at": str(cre or "")[:10],
                 "cover_art_sizes": art or "", "track_count": tc or 0,
                 "favorite_count": saves, "repost_count": reposts,
                 "permalink": f"/{oh}/playlist/{h}" if oh else "",
                 "user": {"id": ohash, "handle": oh or "", "name": on_ or ""}}
            put(playlist_name(h), "Playlist", playlist_fields(d))
            link("released" if alb else "curated", artist_name(ohash), playlist_name(h),
                 "Artist", "Playlist")
            n += 1
    print(f"[dump] playlists {n}", flush=True)

    # ---- pass 5: the social mesh, uncapped -------------------------------------
    social = [
        ("contains", "SELECT playlist_id, track_id FROM playlist_tracks WHERE NOT is_removed",
         playlist_name, track_name, "Playlist", "Track"),
        ("follows", "SELECT follower_user_id, followee_user_id FROM follows WHERE is_current AND NOT is_delete",
         artist_name, artist_name, "Artist", "Artist"),
        ("relatedTo", "SELECT user_id, related_artist_user_id FROM related_artists",
         artist_name, artist_name, "Artist", "Artist"),
        ("supports", "SELECT sender_user_id, receiver_user_id FROM user_tips",
         artist_name, artist_name, "Artist", "Artist"),
        ("hasStem", "SELECT parent_track_id, child_track_id FROM stems",
         track_name, track_name, "Track", "Track"),
        ("favorited", "SELECT user_id, save_item_id FROM saves WHERE is_current AND NOT is_delete AND save_type='track'",
         artist_name, track_name, "Artist", "Track"),
        ("reposted", "SELECT user_id, repost_item_id FROM reposts WHERE is_current AND NOT is_delete AND repost_type='track'",
         artist_name, track_name, "Artist", "Track"),
        ("reposted", "SELECT user_id, repost_item_id FROM reposts WHERE is_current AND NOT is_delete AND repost_type IN ('playlist','album')",
         artist_name, playlist_name, "Artist", "Playlist"),
    ]
    for rel, sql, fname, tname, ft, tt in social:
        before = edges.n
        with cursor(conn, f"soc_{rel}_{ft}_{tt}_{before}") as c:
            # A dev run must not scan 26M follows to produce a handful of edges.
            c.execute(sql + (f" LIMIT {args.limit * 50}" if args.limit else ""))
            for a, b in c:
                ah, bh = encode_id(a), encode_id(b)
                if ah and bh:
                    link(rel, fname(ah), tname(bh), ft, tt)
        print(f"[dump] {rel} {ft}->{tt}: +{edges.n - before}", flush=True)

    for k, wr in writers.items():
        print(f"[dump] volume_{k[0]}_{k[1]}.json: {wr.close()}", flush=True)
    edges.finish()
    print(f"[dump] edges total {edges.n} — A {edges.counts['A']} "
          f"B {edges.counts['B']} linkset {edges.counts['link']}", flush=True)
    json.dump({"focusArtist": args.focus_handle, "idSpace": "name",
               "count": edges.counts["link"]},
              open(os.path.join(args.out_dir, "linkset.meta.json"), "w"))
    conn.close()


if __name__ == "__main__":
    main()
