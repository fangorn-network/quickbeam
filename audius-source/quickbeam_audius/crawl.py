"""The read leg — walk Audius and emit typed raw records.

WHY A CACHE SITS BETWEEN THIS AND `build_graph`
-----------------------------------------------
Fangorn is content-addressed: identical logical content must re-derive an identical
CID, or an unchanged commit uploads its whole graph again. But Audius is *live* —
play counts tick, trending reorders every few minutes. Crawling straight into
`build_graph` would make every run produce a different graph.

So the crawl writes a raw record cache and `build_graph` reads only from that. Same
cache → same nodes → same CIDs → same commit. Re-crawling is then an explicit act
that produces a real, reviewable diff (which is itself worth demoing).

It also means a flaky discovery node can't break the demo mid-pitch — once the cache
exists, nothing downstream touches the network.

RECORD SHAPES
-------------
    {"type": "artist"|"track"|"playlist", "id": <encoded id>, "data": {...}}
    {"type": "rel", "rel", "from", "to", "fromType", "toType", "fields"?: {...}}

Relations that require IO (following, related, supporters, playlist contents, remix
parents, stems) are discovered here. Relations derivable from a node's own fields
(genre/mood/tags) are computed purely in `build_graph` — no IO, so they stay testable.
"""
from __future__ import annotations

from hashids import Hashids

from .api import AudiusAPI

# Audius' public id encoding. `playlists_containing_track` (and a few other fields)
# hand back RAW NUMERIC ids while every API path takes the ENCODED form, so the two
# have to be bridged. Verified against a live pair: 1192383172 <-> "AlQM0Bg".
_HASHIDS = Hashids(salt="azowernasdfoia", min_length=5)


def encode_id(n) -> str | None:
    try:
        return _HASHIDS.encode(int(n))
    except (TypeError, ValueError):
        return None


# -- node naming ------------------------------------------------------------
# Stable ids. These are what edge endpoints reference and what the offline prebake
# path uses as its point id, so they must be stable across runs.
def artist_name(i) -> str:   return f"audius:user:{i}"
def track_name(i) -> str:    return f"audius:track:{i}"
def playlist_name(i) -> str: return f"audius:playlist:{i}"


def _rel(rel, frm, to, ftype, ttype, **fields) -> dict:
    r = {"type": "rel", "rel": rel, "from": frm, "to": to,
         "fromType": ftype, "toType": ttype}
    if fields:
        r["fields"] = fields
    return r


def _owner_id(obj: dict) -> str | None:
    """The encoded id of whoever owns a track/playlist."""
    u = obj.get("user") or {}
    return u.get("id")


class Crawler:
    """Bounded walk of the Audius graph, seeded from trending + one focus artist.

    Every list is capped by an explicit flag: this talks to volunteer-run public
    infrastructure, and an unbounded crawl of a social graph does not terminate.
    """

    def __init__(self, api: AudiusAPI, *, focus_handle: str,
                 max_artists: int = 150, max_trending: int = 200,
                 max_playlists: int = 60, focus_playlists: int = 80,
                 per_artist_tracks: int = 30, workers: int = 4, verbose: bool = True):
        self.api = api
        self.focus_handle = focus_handle
        self.max_artists = max_artists
        self.max_trending = max_trending
        self.max_playlists = max_playlists
        self.focus_playlists = focus_playlists
        self.per_artist_tracks = per_artist_tracks
        self.workers = workers
        self.verbose = verbose

        self.artists: dict[str, dict] = {}
        self.tracks: dict[str, dict] = {}
        self.playlists: dict[str, dict] = {}
        self.rels: list[dict] = []
        self._rel_seen: set[tuple] = set()
        self._pending_favorites: list[tuple[str, str]] = []

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[audius] {msg}", flush=True)

    # -- collectors ---------------------------------------------------------
    def add_artist(self, u: dict | None) -> str | None:
        if not u or not u.get("id"):
            return None
        self.artists.setdefault(u["id"], u)
        return u["id"]

    def add_track(self, t: dict | None) -> str | None:
        if not t or not t.get("id") or t.get("is_delete"):
            return None
        self.tracks.setdefault(t["id"], t)
        self.add_artist(t.get("user"))
        return t["id"]

    def add_playlist(self, p: dict | None) -> str | None:
        if not p or not p.get("id") or p.get("is_delete"):
            return None
        self.playlists.setdefault(p["id"], p)
        self.add_artist(p.get("user"))
        return p["id"]

    def add_rel(self, rel, frm, to, ftype, ttype, **fields) -> None:
        """Dedupe on the triple — the same edge is reached from several directions
        (a track's owner is also found via the artist's own listing)."""
        if not frm or not to or frm == to:
            return
        key = (rel, frm, to)
        if key in self._rel_seen:
            return
        self._rel_seen.add(key)
        self.rels.append(_rel(rel, frm, to, ftype, ttype, **fields))

    # -- the walk -----------------------------------------------------------
    def run(self) -> list[dict]:
        focus = self._seed_focus()
        self._seed_trending()
        self._expand_artists()
        self._expand_playlists(focus)
        self._track_details()
        self._resolve_favorites()
        return self.records()

    def _resolve_favorites(self) -> None:
        """Attach the deferred `favorited` edges, now that every track is known.

        Favorites pointing outside the crawled set are dropped rather than fetched:
        one extra request per favorite would double the crawl to add edges onto
        tracks no other part of the graph references.
        """
        kept = 0
        for me, enc in self._pending_favorites:
            if enc in self.tracks:
                self.add_rel("favorited", me, track_name(enc), "Artist", "Track")
                kept += 1
        self.log(f"favorites: {kept}/{len(self._pending_favorites)} resolved "
                 f"within the crawled set")

    def _seed_focus(self) -> str:
        """The artist we will publish as an independent graph. Seeded FIRST and
        expanded fully — they are the point of the demo and are unlikely to appear
        in trending on any given day."""
        u = self.api.get(f"/v1/users/handle/{self.focus_handle}")
        if not u or not u.get("id"):
            raise SystemExit(f"[audius] focus artist {self.focus_handle!r} not found")
        fid = self.add_artist(u)
        self.log(f"focus artist {u.get('name')} ({self.focus_handle}) = {fid}")

        # Their complete catalog — no cap; this graph should be the whole artist.
        for t in self.api.paged(f"/v1/users/{fid}/tracks", max_items=1000):
            self.add_track(t)
        for p in self.api.paged(f"/v1/users/{fid}/playlists", max_items=200):
            self.add_playlist(p)
        for p in self.api.paged(f"/v1/users/{fid}/albums", max_items=200):
            self.add_playlist(p)
        self.log(f"focus catalog: {len(self.tracks)} track(s), "
                 f"{len(self.playlists)} playlist/album(s)")
        return fid

    def _seed_trending(self) -> None:
        """The platform-side slice: what Audius is surfacing right now."""
        n0 = len(self.tracks)
        for path in ("/v1/tracks/trending", "/v1/tracks/trending/underground"):
            for t in self.api.paged(path, max_items=self.max_trending):
                self.add_track(t)
        for p in self.api.paged("/v1/playlists/trending", max_items=self.max_playlists):
            self.add_playlist(p)
        self.log(f"trending: +{len(self.tracks) - n0} track(s), "
                 f"{len(self.playlists)} playlist(s) total")

    def _expand_artists(self) -> None:
        """Per-artist relation fan-out. This is where most of the mesh comes from.

        The focus artist is always expanded; everyone else is capped by
        --max-artists, taken in discovery order so the set is deterministic.
        """
        ids = list(self.artists.keys())[:self.max_artists]
        focus_id = self.api_focus_id
        if focus_id and focus_id not in ids:
            ids.insert(0, focus_id)
        self.log(f"expanding {len(ids)} artist(s) x 8 relation endpoints "
                 f"({self.workers} workers)")

        def job(uid: str) -> tuple[str, dict]:
            a = self.api
            return uid, {
                "tracks":     a.paged(f"/v1/users/{uid}/tracks",
                                      max_items=self.per_artist_tracks),
                "albums":     a.get_list(f"/v1/users/{uid}/albums", limit=50),
                "playlists":  a.get_list(f"/v1/users/{uid}/playlists", limit=50),
                "following":  a.get_list(f"/v1/users/{uid}/following", limit=50),
                "related":    a.get_list(f"/v1/users/{uid}/related", limit=20),
                "supporting": a.get_list(f"/v1/users/{uid}/supporting", limit=20),
                "reposts":    a.get_list(f"/v1/users/{uid}/reposts", limit=20),
                "favorites":  a.get_list(f"/v1/users/{uid}/favorites", limit=20),
            }

        for uid, res in self.api.parallel(ids, job, workers=self.workers):
            me = artist_name(uid)
            for t in res["tracks"]:
                if self.add_track(t):
                    self.add_rel("created", me, track_name(t["id"]), "Artist", "Track")
            for p in res["albums"]:
                if self.add_playlist(p):
                    self.add_rel("released", me, playlist_name(p["id"]), "Artist", "Playlist")
            for p in res["playlists"]:
                if self.add_playlist(p):
                    self.add_rel("curated", me, playlist_name(p["id"]), "Artist", "Playlist")
            for u in res["following"]:
                if self.add_artist(u):
                    self.add_rel("follows", me, artist_name(u["id"]), "Artist", "Artist")
            for u in res["related"]:
                if self.add_artist(u):
                    self.add_rel("relatedTo", me, artist_name(u["id"]), "Artist", "Artist")
            # $AUDIO tipping — a real economic edge, amount carried on the relation.
            for s in res["supporting"]:
                u = s.get("receiver") or {}
                if self.add_artist(u):
                    self.add_rel("supports", me, artist_name(u["id"]), "Artist", "Artist",
                                 amount=str(s.get("amount") or ""), rank=s.get("rank"))
            for r in res["reposts"]:
                item = r.get("item") or r
                if r.get("item_type") == "playlist" or item.get("playlist_id"):
                    if self.add_playlist(item):
                        self.add_rel("reposted", me, playlist_name(item["id"]),
                                     "Artist", "Playlist")
                elif self.add_track(item):
                    self.add_rel("reposted", me, track_name(item["id"]), "Artist", "Track")
            # `favorites` is the odd one out: it returns bare NUMERIC ids
            # (`favorite_item_id`) with no embedded object, unlike `reposts` which
            # embeds the whole track. Defer these until the crawl is done and the
            # full track set is known, or the edge lands before its endpoint does.
            for f in res["favorites"]:
                if str(f.get("favorite_type", "")).endswith("track"):
                    enc = encode_id(f.get("favorite_item_id"))
                    if enc:
                        self._pending_favorites.append((me, enc))

        self.log(f"after artist fan-out: {len(self.artists)} artist(s), "
                 f"{len(self.tracks)} track(s), {len(self.rels)} relation(s)")

    def _expand_playlists(self, focus_id: str) -> None:
        """Playlist contents — including the platform playlists that contain the
        FOCUS artist's tracks. Those become the cross-graph `contains` edges: a
        playlist curated inside the platform's graph whose contents live in the
        artist's own repo. It is the single most legible moment in the demo.

        `playlists_containing_track` gives raw numeric ids, so they go through
        `encode_id` before they can be fetched.
        """
        wanted = list(self.playlists.keys())
        containing: list[str] = []
        for t in self.tracks.values():
            if _owner_id(t) != focus_id:
                continue
            for raw in (t.get("playlists_containing_track") or []):
                enc = encode_id(raw)
                if enc and enc not in self.playlists and enc not in containing:
                    containing.append(enc)
        containing = containing[:self.focus_playlists]
        self.log(f"playlist contents: {len(wanted)} known + {len(containing)} "
                 f"containing a focus track")

        def job(pid: str) -> tuple[str, dict | None, list]:
            meta = self.api.get_list(f"/v1/playlists/{pid}")
            tracks = self.api.get_list(f"/v1/playlists/{pid}/tracks")
            return pid, (meta[0] if meta else None), tracks

        for pid, meta, tracks in self.api.parallel(wanted + containing, job,
                                                   workers=self.workers):
            # A playlist can 404 its metadata (deleted/private) while still serving
            # contents. Only keep ones we can actually describe.
            if pid not in self.playlists:
                if not self.add_playlist(meta):
                    continue
            p = self.playlists[pid]
            if _owner_id(p):
                self.add_rel("curated" if not p.get("is_album") else "released",
                             artist_name(_owner_id(p)), playlist_name(pid),
                             "Artist", "Playlist")
            for t in tracks:
                if self.add_track(t):
                    self.add_rel("contains", playlist_name(pid), track_name(t["id"]),
                                 "Playlist", "Track")
                    self.add_rel("created", artist_name(_owner_id(t)),
                                 track_name(t["id"]), "Artist", "Track")

        self.log(f"after playlists: {len(self.tracks)} track(s), "
                 f"{len(self.rels)} relation(s)")

    def _track_details(self) -> None:
        """Remix lineage + stems. Only fetched for tracks whose own fields say there
        is something to fetch — `has_remixes` / `remix_of` / `stem_of` — so this
        costs a handful of calls, not one per track."""
        cand = [t for t in self.tracks.values()
                if t.get("has_remixes") or (t.get("remix_of") or {}).get("tracks")
                or t.get("stem_of")]
        if not cand:
            self.log("remix/stem: no flagged tracks")
            return
        self.log(f"remix/stem: {len(cand)} flagged track(s)")

        def job(tid: str) -> tuple[str, list, list]:
            return (tid,
                    self.api.get_list(f"/v1/tracks/{tid}/remixing"),
                    self.api.get_list(f"/v1/tracks/{tid}/stems"))

        for tid, parents, stems in self.api.parallel([t["id"] for t in cand], job,
                                                     workers=self.workers):
            for p in parents:
                if self.add_track(p):
                    self.add_rel("remixOf", track_name(tid), track_name(p["id"]),
                                 "Track", "Track")
            for s in stems:
                if self.add_track(s):
                    self.add_rel("hasStem", track_name(tid), track_name(s["id"]),
                                 "Track", "Track")

    # -- output -------------------------------------------------------------
    @property
    def api_focus_id(self) -> str | None:
        for uid, u in self.artists.items():
            if (u.get("handle") or "").lower() == self.focus_handle.lower():
                return uid
        return None

    def records(self) -> list[dict]:
        """Flatten to the record list `build_graph` consumes. Sorted by id within
        each type so the cache — and therefore every CID downstream — is stable
        regardless of the order threads happened to finish in."""
        out: list[dict] = []
        for i in sorted(self.artists):
            out.append({"type": "artist", "id": i, "data": self.artists[i]})
        for i in sorted(self.tracks):
            out.append({"type": "track", "id": i, "data": self.tracks[i]})
        for i in sorted(self.playlists):
            out.append({"type": "playlist", "id": i, "data": self.playlists[i]})
        out.extend(sorted(self.rels, key=lambda r: (r["rel"], r["from"], r["to"])))
        return out


def crawl(**kw) -> list[dict]:
    api = kw.pop("api", None) or AudiusAPI()
    c = Crawler(api, **kw)
    recs = c.run()
    c.log(f"done — {len(recs)} record(s), {api.calls} API call(s), "
          f"{api.throttled} throttle(s), final delay {api.delay:.2f}s")
    return recs
