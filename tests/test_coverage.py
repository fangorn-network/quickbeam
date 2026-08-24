"""Coverage centroids: the summary that lets a client rank a domain it has NOT
downloaded. If this breaks, auto-mount silently ranks everything the same."""
import numpy as np

from quickbeam.cdn import COVERAGE_DIM, _coverage


def _cluster(axes, n, rng):
    """n unit vectors concentrated on `axes` of a 256-d space."""
    X = rng.normal(0, 0.05, size=(n, 256)).astype(np.float32)
    for a in axes:
        X[:, a] += 1.0
    return X / np.linalg.norm(X, axis=1, keepdims=True)


def test_centroids_route_to_the_right_domain():
    rng = np.random.default_rng(0)
    # Both regions live inside the first COVERAGE_DIM dims ON PURPOSE. Matryoshka
    # truncation keeps the LEADING components; a region out at axis 200 truncates to
    # all zeros and routing stops working with no error at all.
    cooking = _cluster([1, 2, 3], 300, rng)
    markets = _cluster([60, 61, 62], 300, rng)

    cov_c = _coverage(cooking.tolist())
    cov_m = _coverage(markets.tolist())
    assert cov_c["dim"] == COVERAGE_DIM
    assert sum(cov_c["counts"]) == 300, "every sampled vector must land in a cell"

    def affinity(cov, q):
        q = np.asarray(q[: cov["dim"]], dtype=np.float32)
        q /= np.linalg.norm(q) or 1.0
        return float(np.max(np.asarray(cov["vectors"], dtype=np.float32) @ q))

    q = _cluster([1, 2, 3], 1, rng)[0]
    assert affinity(cov_c, q) > affinity(cov_m, q) + 0.3, "a cooking query must name the cooking domain"

    for cov in (cov_c, cov_m):
        norms = np.linalg.norm(np.asarray(cov["vectors"], dtype=np.float32), axis=1)
        assert np.allclose(norms, 1.0, atol=1e-3), "truncated centroids must be renormalized — cosine assumes it"


def test_empty_domain_publishes_no_coverage():
    # A domain with no vectors must omit the key, not publish a zero centroid that
    # would tie with every query.
    assert _coverage([]) is None
