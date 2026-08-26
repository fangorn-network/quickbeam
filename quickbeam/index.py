"""Codebook, quantization and the privacy/recall measurement harness.

This is the numerics half of `quickbeam cdn index`; `cdn.py` keeps the argparse and
the file writing, mirroring how `_project_umap` is a lazily-imported free function
rather than logic living inside the bake.

WHAT THIS IS FOR
----------------
Serving a 2.5M+ corpus from a hosted vector DB means the query leaves the client, and
an embedding is not an opaque token — vec2text-style inversion reconstructs short
inputs near-exactly, and the noise levels that preserve retrieval do not stop it. So
the client never sends a vector. Instead:

  1. the client embeds its query locally and finds the nearest centroid in a PUBLIC
     codebook (this file fits it),
  2. it sends only the BUCKET that centroid falls into — `hash(cell) % nbuckets`,
     baked in and identical for every client,
  3. the server returns the top-k' candidates for each cell in that bucket,
  4. the client re-ranks those candidates against its true float query.

The disclosed value is a deterministic, public function of the query. That is the
whole point: per-query client randomness (a rotated vector, freshly-sampled decoy
cells) is defeated by intersection across a session, because a session's true queries
are correlated — `kernelRecommend` fires on every rail bump over an EMA-smoothed mu —
while fresh noise is not. Determinism has no such seam, and it caches.

Bucket membership is HASH-SCATTERED rather than coarsened, which is load-bearing.
A geometrically contiguous cell is semantically coherent, and coherence is exactly
what we are trying not to disclose: measured on the 25k corpus at K=512, cell id
predicts entityType at 0.985 and genre at 0.833 (against 0.438/0.201 majority
baselines). Scattering b unrelated cells into one bucket flattens that posterior
without touching recall, because recall is set by the FINE cell and privacy by the
scatter. Two independent integer dials.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
The codebook is FLAT, not the two-level tree the design calls for. The tree
is a client-side routing accelerator — it finds the same nearest centroid, just in
~512 comparisons instead of K — so it cannot change any number this harness reports.
Fit it in Stage D from the flat codebook, when there is a client that cares.
"""
import gzip
import hashlib
import json
import os

import numpy as np

# The measurement is only trustworthy against real query vectors. nomic is asymmetric
# (`search_query:` vs `search_document:`), so queries sit off the document manifold and
# may land between cells; held-out documents as queries is the optimistic case.
QUERY_PREFIX = "search_query: "


# ---------------------------------------------------------------------------
# CORPUS IO — read vectors back out of the baked shards
# ---------------------------------------------------------------------------
def load_corpus(domain_dir: str, limit: int = 0):
    """Load a baked domain's vectors + the payload fields the report needs.

    Reads the shards rather than Qdrant so this is re-runnable against a fixture
    directory with no live service — the same choice `_existing_baked_ids` makes.
    Returns (ids, X[N,D] float32 unit-norm, fields[N])."""
    manifest_path = os.path.join(domain_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        raise SystemExit(f"[index] not baked: {manifest_path} — run `cdn bake` first")
    with open(manifest_path) as f:
        manifest = json.load(f)

    ids: list[str] = []
    fields: list[dict] = []
    vecs: list[np.ndarray] = []
    for s in manifest.get("shards", []):
        path = os.path.join(domain_dir, s["file"])
        if not os.path.exists(path):
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 - skip a corrupt line, keep going
                    continue
                emb = row.get("embedding")
                if not emb:
                    continue
                ids.append(row["track_id"])
                fields.append(row.get("fields") or {})
                vecs.append(np.asarray(emb, dtype=np.float32))
                if limit and len(ids) >= limit:
                    break
        if limit and len(ids) >= limit:
            break

    if not ids:
        raise SystemExit(f"[index] no embeddings found in {domain_dir}")
    X = np.vstack(vecs)
    # The bake already stores unit-norm vectors (identity.matryoshka L2-normalizes),
    # but re-normalize so a hand-assembled fixture can't quietly skew every cosine.
    X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-12)
    return ids, X, fields


# ---------------------------------------------------------------------------
# CODEBOOK
# ---------------------------------------------------------------------------
def _row_chunk(k: int, budget: int = 20_000_000) -> int:
    """Rows per block so the [chunk, K] score matrix stays near `budget` floats.

    Fixed chunk sizes do not survive this workload: the score matrix is chunk*K, so
    a 100k-row block that costs 400 MB at K=512 costs 40 GB at K=10000. Sizing off K
    keeps every block bounded regardless of how fine the codebook gets."""
    return int(max(256, min(100_000, budget // max(k, 1))))


def _assign_chunked(X, C, chunk=None):
    """argmax cosine against the codebook, in row blocks so the [N,K] score matrix
    is never materialized (at N=500k, K=4096 that would be 8 GB)."""
    chunk = chunk or _row_chunk(len(C))
    out = np.empty(len(X), dtype=np.int32)
    for i in range(0, len(X), chunk):
        out[i:i + chunk] = np.argmax(X[i:i + chunk] @ C.T, axis=1)
    return out


def spherical_kmeans(X, k: int, iters: int = 25, seed: int = 0, chunk: int = 100_000):
    """Spherical k-means: assign by max dot product, update by mean + renormalize.

    Everything here is unit-norm and scored by cosine, so this is the matching
    algorithm — Euclidean k-means on a sphere optimizes the wrong objective at the
    margins. sklearn is not a declared dependency of quickbeam (it is only present
    transitively under umap-learn), and the balanced assignment below is something
    sklearn cannot do anyway, so both halves are written out.

    Full-batch, chunked. At 500k x 4096 that is ~10s/iteration, fine for
    the gate. If this is still the fit path at 20M, switch to minibatch (sample a
    batch per iteration, same update rule) — the ceiling is wall-clock, not quality.
    """
    rng = np.random.default_rng(seed)
    # k-means++ is not worth it here: the corpus is dense on the sphere and 25
    # iterations from a random sample lands in the same place. Distinct rows only,
    # so a duplicated vector cannot seed two identical centroids.
    start = rng.choice(len(X), size=min(k, len(X)), replace=False)
    C = X[start].copy()

    for _ in range(iters):
        labels = _assign_chunked(X, C, chunk)
        # Sum members per centroid without a Python loop over k.
        sums = np.zeros_like(C)
        np.add.at(sums, labels, X)
        counts = np.bincount(labels, minlength=len(C))
        # An empty cell keeps its old centroid rather than collapsing to the origin.
        alive = counts > 0
        C[alive] = sums[alive]
        C /= np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-12)
    return C


def assign_balanced(X, C, cap_factor: float = 1.5, chunk: int | None = None,
                    shortlist: int = 64):
    """Assign every row to a cell, capping cell size at `cap_factor * mean` and
    spilling overflow to the next-nearest cell with room.

    Raw k-means skews hard — measured on the 25k corpus at K=512: min 1, median 32,
    p95 150, max 772. That is a 24x spread, and it hurts twice. Fetch size becomes
    unpredictable, and a rare cell is far more identifying than a common one, so the
    anonymity set a bucket claims to provide is not the one it actually provides.
    Balancing costs no recall at equal scan fraction; it buys predictability and a
    flat prior."""
    n, k = len(X), len(C)
    chunk = chunk or _row_chunk(k)
    cap = max(1, int(np.ceil(cap_factor * n / k)))
    m = int(min(k, shortlist))
    labels = np.full(n, -1, dtype=np.int32)
    counts = np.zeros(k, dtype=np.int32)

    for i in range(0, n, chunk):
        scores = X[i:i + chunk] @ C.T
        # Only the m nearest cells are ranked. A full argsort of every row is what
        # this used to do, and it does not survive a fine codebook — at K=10000 the
        # index array alone is 40 GB for a 500k corpus. Spilling past the 64th
        # nearest cell is vanishingly rare, and the exact fallback below covers it.
        part = np.argpartition(-scores, m - 1, axis=1)[:, :m]
        rows = np.arange(len(scores))[:, None]
        order = part[rows, np.argsort(-scores[rows, part], axis=1)]
        for r in range(len(scores)):
            for c in order[r]:
                if counts[c] < cap:
                    labels[i + r] = c
                    counts[c] += 1
                    break
            else:
                # Shortlist exhausted: rank the whole row for this one record rather
                # than dropping it into an arbitrary cell.
                for c in np.argsort(-scores[r]):
                    if counts[c] < cap:
                        labels[i + r] = c
                        counts[c] += 1
                        break
                else:
                    # Every cell full (reachable only when cap*k == n exactly): put it
                    # in its nearest and let that one run over rather than lose a row.
                    c = int(order[r][0])
                    labels[i + r] = c
                    counts[c] += 1
    return labels, counts


def load_layout(path: str) -> dict:
    """Read a baked `index/layout.json`. Shared by the server and any tooling, so
    the bucket->cells expansion has one definition."""
    with open(path) as f:
        return json.load(f)


def cells_in_bucket(layout: dict, bucket: int) -> list[int]:
    """The cells a bucket id expands to.

    The SERVER does this expansion, not the client, and that is deliberate: it means
    a caller cannot request a single cell and thereby opt out of its own anonymity
    set. The smallest thing anyone can ask for is a whole bucket."""
    return [c for c, b in enumerate(layout["buckets"]) if int(b) == int(bucket)]


def bucket_map(k: int, nbuckets: int, salt: str = "quickbeam-v1") -> np.ndarray:
    """cell -> bucket, hash-scattered and deterministic.

    Public, baked, identical for every client: there is no client entropy anywhere in
    the disclosed path, which is what removes the intersection attack.

    Two properties, and the second one is easy to lose. The map must SCATTER — cells
    adjacent in k-means fit order are adjacent in geometry, so `cell % nbuckets` would
    partially reconstruct the contiguity the bucket exists to destroy. And it must be
    BALANCED: a salted-hash modulo gives Poisson-spread bucket sizes (measured 2..16
    for a nominal 8), so an unlucky query discloses itself into an anonymity set a
    quarter of the advertised size, and the server can see from the public map which
    buckets those are. Dealing a deterministically shuffled cell order round-robin
    gives both — every bucket holds exactly floor(k/nbuckets) or one more, so the
    advertised b is the b every user actually gets."""
    seed = int.from_bytes(hashlib.sha256(salt.encode()).digest()[:8], "big")
    order = np.random.default_rng(seed).permutation(k)
    out = np.empty(k, dtype=np.int32)
    out[order] = np.arange(k) % nbuckets
    return out


# ---------------------------------------------------------------------------
# QUANTIZATION — must reproduce byte-for-byte in TS (src/lib/quant.ts)
# ---------------------------------------------------------------------------
def int8_encode(X, scale: float | None = None):
    """Symmetric int8 with one global scale. Returns (codes int8, scale).

    No zero-point: the vectors are unit-norm and centered near zero, so a symmetric
    code wastes nothing and stays trivially reproducible cross-language. Measured
    cost on the 25k corpus is R@10 0.956 / R@1 0.954 against exact fp32 — which is
    why candidates come back as int8 and NOT as sign bits (binary-256 as a final
    ranker measured 0.398; every good binary number in the literature assumes a float
    re-rank that a bits-only wire format has thrown away)."""
    if scale is None:
        scale = float(np.max(np.abs(X))) or 1.0
    codes = np.rint(X / scale * 127.0).clip(-127, 127).astype(np.int8)
    return codes, scale


def int8_decode(codes, scale: float):
    return codes.astype(np.float32) * (scale / 127.0)


def sign_encode(X) -> np.ndarray:
    """Packed sign bits, MSB-first within each byte. sign(0) = +1, pinned here and in
    quant.ts — ~0.4% of vectors carry a component within 1e-6 of zero, and an
    unpinned tie flips a bit between the two implementations."""
    bits = (X >= 0).astype(np.uint8)
    return np.packbits(bits, axis=1, bitorder="big")


# ---------------------------------------------------------------------------
# MEASUREMENT — the gate
# ---------------------------------------------------------------------------
def _topk(scores, k):
    k = min(k, len(scores))
    idx = np.argpartition(-scores, k - 1)[:k]
    return idx[np.argsort(-scores[idx])]


def embed_queries(texts: list[str], dim: int):
    """Embed real query strings through the same model + transform the client uses.

    Lazily imported so the rest of this module runs with no fastembed install, the
    same shape as `_project_umap`'s umap-learn import."""
    from fastembed import TextEmbedding

    from quickbeam.ingest.identity import matryoshka

    model = TextEmbedding(model_name="nomic-ai/nomic-embed-text-v1.5")
    raw = list(model.embed([QUERY_PREFIX + t for t in texts]))
    return np.vstack([np.asarray(matryoshka(v, dim), dtype=np.float32) for v in raw])


def recall_report_cells(X, C, labels, bmap, queries, nprobe: int, k_eval: int = 10):
    """Recall when the server returns a cell's MEMBERS rather than ANN results for
    its centroid.

    The distinction is the whole ballgame. Asking the server for "top-k' nearest the
    centroid" makes the centroid the query, and a centroid is the mean of its members
    — in high dimensions that mean is a poor stand-in for any individual query, which
    is why the centroid path needs a quarter of the corpus as candidates before it
    reaches 0.95 even with no bucket at all. Asking for "the members of cell c" is
    exact: the client re-ranks them against its true query and loses nothing except
    what fell outside the probed cells. Same disclosure either way — a bucket id —
    so if this wins it wins for free.

    The client probes its `nprobe` nearest cells and discloses the BUCKETS those fall
    into; the server returns every member of every cell in those buckets."""
    members: dict[int, np.ndarray] = {
        int(c): np.flatnonzero(labels == c) for c in np.unique(labels)
    }
    q8, scale = int8_encode(X)
    Xq = int8_decode(q8, scale)
    Xq /= np.maximum(np.linalg.norm(Xq, axis=1, keepdims=True), 1e-12)

    hits10 = hits1 = 0
    cand_sizes = []
    for q in queries:
        truth = _topk(X @ q, k_eval)
        probes = _topk(C @ q, nprobe)
        buckets = np.unique(bmap[probes])
        cells = np.flatnonzero(np.isin(bmap, buckets))
        cand = np.concatenate([members[int(c)] for c in cells if len(members.get(int(c), ()))])
        cand_sizes.append(len(cand))

        ranked = cand[_topk(Xq[cand] @ q, k_eval)]
        hits10 += len(np.intersect1d(ranked, truth)) / float(k_eval)
        hits1 += 1.0 if len(ranked) and ranked[0] == truth[0] else 0.0

    n = len(queries)
    return {
        "r_at_10": hits10 / n, "r_at_1": hits1 / n,
        "mean_candidates": float(np.mean(cand_sizes)),
        "scan_frac": float(np.mean(cand_sizes)) / len(X),
        "bucket_size": int(np.bincount(bmap).mean()),
    }


def recall_report(X, C, labels, bmap, queries, kprime: int, k_eval: int = 10):
    """End-to-end recall of the private path against exhaustive fp32.

    Simulates exactly what ships: nearest centroid found locally, the whole bucket
    disclosed, the server returning top-(kprime/b) per cell in that bucket, and the
    client re-ranking the union against its TRUE float query using int8-decoded
    document vectors. Every loss in the pipeline is therefore in the number —
    quantization to a cell, the bucket's split budget, and int8."""
    q8, scale = int8_encode(X)
    Xq = int8_decode(q8, scale)
    Xq /= np.maximum(np.linalg.norm(Xq, axis=1, keepdims=True), 1e-12)

    # Per-cell candidate lists are reused across queries that land in the same bucket,
    # which is the common case — cache them rather than rescanning the corpus.
    cell_cache: dict[tuple[int, int], np.ndarray] = {}
    hits10 = hits1 = 0
    cand_sizes = []

    for q in queries:
        truth = _topk(X @ q, k_eval)
        cell = int(np.argmax(C @ q))
        bucket = int(bmap[cell])
        cells = np.flatnonzero(bmap == bucket)
        per_cell = max(1, kprime // len(cells))

        cand = []
        for c in cells:
            key = (int(c), per_cell)
            if key not in cell_cache:
                cell_cache[key] = _topk(X @ C[c], per_cell)
            cand.append(cell_cache[key])
        cand = np.unique(np.concatenate(cand))
        cand_sizes.append(len(cand))

        ranked = cand[_topk(Xq[cand] @ q, k_eval)]
        hits10 += len(np.intersect1d(ranked, truth)) / float(k_eval)
        hits1 += 1.0 if len(ranked) and ranked[0] == truth[0] else 0.0

    n = len(queries)
    return {
        "r_at_10": hits10 / n,
        "r_at_1": hits1 / n,
        "mean_candidates": float(np.mean(cand_sizes)),
        "bucket_size": int(np.bincount(bmap).mean()),
    }


def purity_report(labels, bmap, fields, key: str):
    """How well a disclosed unit predicts `key` (e.g. 'genre', 'entityType').

    Reported at BOTH granularities on purpose. Cell purity is what a naive
    contiguous-cell disclosure would leak; bucket purity is what actually goes on the
    wire. The gap between them is the entire value of hash-scattering, and the bucket
    number is the one the privacy page has to quote."""
    # Only records that HAVE the field. Most of this corpus is Tags/Artists/Playlists
    # with no genre, and letting the empty string be a class makes "" the majority
    # everywhere — the purity number then measures how well a cell predicts "has no
    # genre", which is not the disclosure anyone cares about.
    keep = np.asarray([bool((f or {}).get(key)) for f in fields])
    if not keep.any():
        return {"field": key, "classes": 0, "coverage": 0.0,
                "majority_baseline": 0.0, "cell_purity": 0.0, "bucket_purity": 0.0}
    vals = [str(fields[i][key]) for i in np.flatnonzero(keep)]
    labels = labels[keep]
    uniq = {v: i for i, v in enumerate(sorted(set(vals)))}
    y = np.asarray([uniq[v] for v in vals], dtype=np.int32)
    nclass = len(uniq)

    def _purity(groups):
        total = 0
        for g in np.unique(groups):
            m = y[groups == g]
            if len(m):
                total += np.bincount(m, minlength=nclass).max()
        return total / len(y)

    baseline = np.bincount(y, minlength=nclass).max() / len(y)
    return {
        "field": key,
        "classes": nclass,
        "coverage": float(keep.mean()),
        "majority_baseline": float(baseline),
        "cell_purity": float(_purity(labels)),
        "bucket_purity": float(_purity(bmap[labels])),
    }


def scale_sweep(X, sizes, records_per_cell: int, budget: int, nqueries: int,
                cap_factor: float = 1.5, iters: int = 20, seed: int = 0,
                mode: str = "centroid"):
    """R@10 as the corpus grows, with records-per-cell held constant.

    This is the gate's real question. Every absolute number measured on the 25k
    corpus is in a degenerate regime — K=512 there means ~50 records per cell, and a
    centroid over 50 records is a poor proxy for a query. The level does not transfer
    to 2.5M, but the TREND does, and the trend is what says whether the approach gets
    better or worse as the corpus grows. Scaling K with N is what keeps the only
    variable the corpus size."""
    rng = np.random.default_rng(seed)
    out = []
    for n in sizes:
        n = min(int(n), len(X))
        sub = X[rng.choice(len(X), size=n, replace=False)]
        k = max(8, n // records_per_cell)
        C = spherical_kmeans(sub, k, iters=iters, seed=seed)
        labels, _ = assign_balanced(sub, C, cap_factor=cap_factor)
        bmap = bucket_map(k, max(1, k // 8))
        q = sub[rng.choice(n, size=min(nqueries, n), replace=False)]
        fn = recall_report_cells if mode == "cell" else recall_report
        r = fn(sub, C, labels, bmap, q, budget)
        r.update(n=n, k=k)
        out.append(r)
    return out
