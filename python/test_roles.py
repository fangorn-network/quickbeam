git remote add origin git@github.com:fangorn-network/embeddings.git"""Parity + generalization checks for role inference. Run: python test_roles.py"""
from roles import infer_roles

MUSIC = [{
    "schemaVersion": 1,
    "trackId": "abc123def456",
    "isrcCode": "USRC17607830",
    "title": "Lagos at 3am",
    "byArtist": "Tunde Okafor",
    "albumName": "Neon Harmattan",
    "datePublished": "2021",
    "durationMs": 252000,
    "genres": ["Afrobeats", "Electronic"],
    "moods": ["energetic", "warm"],
    "themes": ["nightlife"],
    "contexts": ["party"],
    "platform": "spotify",
    "platformId": "3n3Ppam7vgaVa1iaRUc9Lp",
} for _ in range(5)]

OSM = [{
    "changeset_id": "123456789",
    "user_id": "mapper_jane",
    "created_at": "2025-05-12T14:33:01Z",
    "bbox": {"min_lon": -97.5, "min_lat": 32.5, "max_lon": -96.5, "max_lat": 33.2},
    "tags": ["highway", "residential", "building"],
    "comment": "Added missing residential streets in north Dallas and tagged a "
               "few new buildings near the highway interchange for clarity.",
    "num_changes": 42,
} for _ in range(5)]


def check(name, rm, expected):
    ok = True
    for role, want in expected.items():
        got = rm.get(role)
        got_cmp = sorted(got) if isinstance(got, list) else got
        want_cmp = sorted(want) if isinstance(want, list) else want
        mark = "✓" if got_cmp == want_cmp else "✗ FAIL"
        if got_cmp != want_cmp:
            ok = False
        print(f"  {mark} {role:9} = {got!r}  (want {want!r})")
    print(f"  labels: {rm.get('labels')}")
    print(f"[{name}] {'PASS' if ok else 'FAILED'}\n")
    return ok


music_ok = check("music", infer_roles(MUSIC), {
    "identity": "trackId",
    "title": "title",
    "subtitle": "byArtist",
    "tags": ["genres", "moods", "themes", "contexts"],
    "temporal": "datePublished",
    "spatial": None,
    "measures": ["durationMs"],
    "media": "platformId",
    "relations": ["albumName"],
})

osm_ok = check("osm", infer_roles(OSM), {
    "identity": "changeset_id",
    "subtitle": "user_id",
    "tags": ["tags"],
    "temporal": "created_at",
    "spatial": "bbox",
    "measures": ["num_changes"],
    "text": ["comment"],
    "media": None,
})

import sys
sys.exit(0 if (music_ok and osm_ok) else 1)
