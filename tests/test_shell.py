"""Guards for the Python mirror of the client's shell summaries.

`check-graph.ts` asserts baked == computed against a live server and is the real
equivalence proof. These pin the JS semantics that are easy to get subtly wrong in
Python and that no assertion would catch as a *type* error — they would just produce
slightly different numbers than the browser.
"""
from quickbeam.shell import build_stats, onboarding_options, sample

PLATFORM = "0xplatform"
ARTIST = "0xartist"


def rec(rid, etype, owner, **fields):
    return {"id": rid, "entityType": etype, "owner": owner, "fields": fields}


def test_playcount_zero_does_not_fall_through_to_followers():
    """JS `playCount ?? followerCount` is NULLISH — a playCount of 0 is a value and
    must NOT fall through. Using `or` here would rank a 0-play track by its artist's
    follower count and reorder the Home rows."""
    recs = [
        rec("a", "Track", PLATFORM, playCount=0, followerCount=9_000_000),
        rec("b", "Track", PLATFORM, playCount=5),
    ]
    assert [r["id"] for r in sample(recs, "Track", 2, PLATFORM)] == ["b", "a"]


def test_missing_playcount_does_fall_through():
    recs = [
        rec("a", "Track", PLATFORM, followerCount=100),
        rec("b", "Track", PLATFORM, playCount=5),
    ]
    assert [r["id"] for r in sample(recs, "Track", 2, PLATFORM)] == ["a", "b"]


def test_sample_owner_match_is_case_insensitive():
    recs = [rec("a", "Track", "0xABC", playCount=1)]
    assert len(sample(recs, "Track", 5, "0xabc")) == 1
    assert len(sample(recs, "Track", 5, "0xdef")) == 0


def test_platform_sorts_first_regardless_of_size():
    """The ledger reads platform -> artist. A plain total-descending sort would flip
    it whenever the artist publisher happens to be larger."""
    recs = [rec("p", "Track", PLATFORM)] + [rec(f"a{i}", "Track", ARTIST) for i in range(5)]
    stats = build_stats(recs, [], PLATFORM)
    assert [p["owner"] for p in stats["publishers"]] == [PLATFORM, ARTIST]


def test_publisher_labels_itself_from_a_single_artist_record():
    recs = [
        rec("art", "Artist", ARTIST, title="Disclosure"),
        rec("t", "Track", ARTIST),
        # Two Artist records on the platform side -> no label, by design.
        rec("p1", "Artist", PLATFORM, title="A"),
        rec("p2", "Artist", PLATFORM, title="B"),
    ]
    stats = build_stats(recs, [], PLATFORM)
    by_owner = {p["owner"]: p for p in stats["publishers"]}
    assert by_owner[ARTIST]["label"] == "Disclosure"
    assert by_owner[ARTIST]["labelId"] == "art"
    assert "label" not in by_owner[PLATFORM]


def test_reference_stubs_do_not_name_a_publisher():
    """The platform keeps a thin `isReference` stub for the sovereign artist. If that
    counted, the platform would label itself with the artist's name."""
    recs = [rec("stub", "Artist", PLATFORM, title="Disclosure", isReference=True)]
    stats = build_stats(recs, [], PLATFORM)
    assert "label" not in stats["publishers"][0]


def test_linkset_excludes_vocabulary_endpoints():
    """Converged Genre/Mood/Tag vertices collapse to one record with one owner, so
    every other publisher's edge into them LOOKS cross-publisher. That inflated this
    count from 113 to 12,657 once."""
    recs = [
        rec("t1", "Track", PLATFORM), rec("t2", "Track", ARTIST),
        rec("g", "Genre", PLATFORM, vocabulary=True),
    ]
    edges = [
        {"rel": "sameAs", "from": "t1", "to": "t2"},      # real crossing
        {"rel": "inGenre", "from": "t2", "to": "g"},      # vocabulary — must not count
    ]
    stats = build_stats(recs, edges, PLATFORM)
    assert stats["linksetTotal"] == 1
    assert stats["linkset"] == [{"rel": "sameAs", "count": 1}]


def test_converged_counts_vocabulary_reached_by_both_publishers():
    recs = [
        rec("t1", "Track", PLATFORM), rec("t2", "Track", ARTIST),
        rec("shared", "Genre", PLATFORM, vocabulary=True),
        rec("lonely", "Genre", PLATFORM, vocabulary=True),
    ]
    edges = [
        {"rel": "inGenre", "from": "t1", "to": "shared"},
        {"rel": "inGenre", "from": "t2", "to": "shared"},
        {"rel": "inGenre", "from": "t1", "to": "lonely"},
    ]
    assert build_stats(recs, edges, PLATFORM)["converged"] == 1


def test_onboarding_drops_genres_with_no_tracks_and_thin_artists():
    recs = [
        rec("g1", "Genre", PLATFORM, title="House"),
        rec("g2", "Genre", PLATFORM, title="Empty"),
        rec("a1", "Artist", PLATFORM, artist="Big", followerCount=10),
        rec("a2", "Artist", PLATFORM, artist="Thin", followerCount=99),
    ] + [rec(f"t{i}", "Track", PLATFORM) for i in range(3)]
    edges = [{"rel": "inGenre", "from": f"t{i}", "to": "g1"} for i in range(3)]
    edges += [{"rel": "created", "from": "a1", "to": f"t{i}"} for i in range(3)]
    edges += [{"rel": "created", "from": "a2", "to": "t0"}]   # 1 track < MIN_SEED_TRACKS
    out = onboarding_options(recs, edges)
    assert [g["id"] for g in out["genres"]] == ["g1"]
    assert [a["id"] for a in out["artists"]] == ["a1"]


def test_onboarding_ignores_edges_pointing_outside_the_corpus():
    """An edge whose other endpoint was not delivered is not traversable in the
    client either, so counting it would advertise a genre that renders empty."""
    recs = [rec("g1", "Genre", PLATFORM, title="House")]
    edges = [{"rel": "inGenre", "from": "missing", "to": "g1"}]
    assert onboarding_options(recs, edges)["genres"] == []
