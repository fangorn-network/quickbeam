"""Guards for the private-retrieval codebook.

Each of these pins a property that fails SILENTLY — wrong numbers, or a weaker
privacy guarantee than the one the copy claims, with nothing raising.
"""
import numpy as np
import pytest

from quickbeam.index import (
    assign_balanced,
    bucket_map,
    int8_decode,
    int8_encode,
    purity_report,
    recall_report,
    recall_report_cells,
    sign_encode,
    spherical_kmeans,
)


@pytest.fixture
def corpus():
    """Clustered unit vectors — k-means has something real to find."""
    rng = np.random.default_rng(0)
    centers = rng.normal(size=(12, 32)).astype(np.float32)
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    X = np.repeat(centers, 60, axis=0) + rng.normal(scale=0.25, size=(720, 32))
    X = X.astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    return X


def test_int8_roundtrip_is_close(corpus):
    codes, scale = int8_encode(corpus)
    back = int8_decode(codes, scale)
    back /= np.linalg.norm(back, axis=1, keepdims=True)
    # Cosine against the original must survive quantization; this is the number the
    # whole re-rank stage depends on.
    assert np.min(np.sum(back * corpus, axis=1)) > 0.999


def test_sign_of_zero_is_positive():
    """Pinned in both this and src/lib/quant.ts. An unpinned tie flips a bit between
    the two implementations and silently perturbs Hamming distance."""
    x = np.zeros((1, 8), dtype=np.float32)
    assert sign_encode(x)[0][0] == 0xFF


def test_bucket_map_is_deterministic_and_balanced():
    b = bucket_map(512, 64)
    assert np.array_equal(b, bucket_map(512, 64)), "must be reproducible from the salt"
    assert not np.array_equal(b, bucket_map(512, 64, salt="other"))
    sizes = np.bincount(b)
    # Every bucket within one of the mean: the advertised anonymity set must be the
    # one every user actually gets, not an average over a Poisson spread.
    assert sizes.max() - sizes.min() <= 1


def test_bucket_map_scatters_adjacent_cells():
    """k-means numbers cells in fit order, which correlates with geometry. Adjacent
    cells landing in one bucket would rebuild the contiguity the bucket destroys."""
    b = bucket_map(512, 64)
    adjacent_collisions = np.sum(b[:-1] == b[1:])
    assert adjacent_collisions < 25, "scatter is too weak — buckets track fit order"


def test_assign_balanced_respects_cap(corpus):
    C = spherical_kmeans(corpus, 12, iters=10)
    labels, counts = assign_balanced(corpus, C, cap_factor=1.5)
    cap = int(np.ceil(1.5 * len(corpus) / 12))
    assert counts.max() <= cap
    assert counts.sum() == len(corpus)
    assert set(np.unique(labels)) <= set(range(12))


def test_cell_mode_beats_random_and_improves_with_nprobe(corpus):
    C = spherical_kmeans(corpus, 24, iters=15)
    labels, _ = assign_balanced(corpus, C)
    bmap = bucket_map(24, 6)
    q = corpus[::40]

    lo = recall_report_cells(corpus, C, labels, bmap, q, nprobe=1)
    hi = recall_report_cells(corpus, C, labels, bmap, q, nprobe=8)
    assert lo["r_at_10"] > 0.2, "worse than this is not retrieval"
    assert hi["r_at_10"] > lo["r_at_10"], "more probes must not hurt recall"
    assert hi["mean_candidates"] > lo["mean_candidates"]


def test_centroid_mode_reranks_against_the_true_query(corpus):
    """The re-rank is the load-bearing step: candidates are chosen by a centroid but
    ORDERED by the real query. Without it, R@1 collapses to centroid quality."""
    C = spherical_kmeans(corpus, 24, iters=15)
    labels, _ = assign_balanced(corpus, C)
    bmap = bucket_map(24, 6)
    r = recall_report(corpus, C, labels, bmap, corpus[::40], kprime=400)
    assert r["r_at_1"] > 0.5


def test_purity_ignores_records_missing_the_field():
    """Most records have no genre. Letting "" be a class makes it the majority
    everywhere, and the metric silently becomes "predicts whether genre is absent"."""
    labels = np.array([0, 0, 1, 1])
    bmap = np.array([0, 0])
    fields = [{"genre": "house"}, {"genre": "house"}, {}, {"genre": None}]
    p = purity_report(labels, bmap, fields, "genre")
    assert p["classes"] == 1
    assert p["coverage"] == 0.5
