// The query-side vector transform. This MUST match the document side exactly or
// search silently degrades to near-random ranking — nothing errors, results just
// quietly stop being relevant, which is the worst possible failure mode for a demo.
//
// Document side (quickbeam/ingest/identity.py `matryoshka`, via fastembed):
//   embed("search_document: " + text) -> layer-norm across all 768 dims
//                                     -> slice to 256 -> L2-normalize
// Query side (here): the SAME transform on "search_query: " + text.
//
// The prefixes are not decoration: nomic-embed-text-v1.5 is an ASYMMETRIC model and
// expects queries and documents to be marked differently. Dropping the prefix costs
// real ranking quality.
//
// Layer-norm is scale-invariant, so it does not matter that a given runtime
// mean-pools without L2-normalizing first.

export const MATRYOSHKA_DIM = 256;
export const QUERY_PREFIX = 'search_query: ';
export const DOCUMENT_PREFIX = 'search_document: ';

export function matryoshka(vec: Float32Array | number[], dim = MATRYOSHKA_DIM): Float32Array {
  const n = vec.length;
  let mean = 0;
  for (let i = 0; i < n; i++) mean += vec[i];
  mean /= n;

  let varsum = 0;
  for (let i = 0; i < n; i++) {
    const d = vec[i] - mean;
    varsum += d * d;
  }
  // +1e-5 matches numpy's epsilon in the Python implementation.
  const std = Math.sqrt(varsum / n + 1e-5);

  const out = new Float32Array(Math.min(dim, n));
  for (let i = 0; i < out.length; i++) out[i] = (vec[i] - mean) / std;

  let norm = 0;
  for (const x of out) norm += x * x;
  norm = Math.sqrt(norm);
  if (norm) for (let i = 0; i < out.length; i++) out[i] /= norm;
  return out;
}

// Cosine against a flat, contiguous Float32Array of unit-length document vectors.
// Both sides are L2-normalized, so the dot product IS the cosine — no division, and
// one flat buffer instead of 25k small arrays keeps this cache-friendly enough to
// stay well inside a frame budget even before it moved off the main thread.
export function topK(
  flat: Float32Array,
  count: number,
  query: Float32Array,
  k: number,
  accept?: (i: number) => boolean,
): Array<{ index: number; score: number }> {
  const dim = query.length;
  const best: Array<{ index: number; score: number }> = [];
  let floor = -Infinity;
  for (let i = 0; i < count; i++) {
    if (accept && !accept(i)) continue;
    const off = i * dim;
    let dot = 0;
    for (let d = 0; d < dim; d++) dot += flat[off + d] * query[d];
    if (best.length < k) {
      best.push({ index: i, score: dot });
      if (best.length === k) {
        best.sort((a, b) => b.score - a.score);
        floor = best[best.length - 1].score;
      }
    } else if (dot > floor) {
      best[best.length - 1] = { index: i, score: dot };
      // Insertion sort: the list is tiny (k) and already ordered, so this is
      // cheaper than re-sorting and avoids allocating on the hot path.
      for (let j = best.length - 1; j > 0 && best[j].score > best[j - 1].score; j--) {
        const t = best[j]; best[j] = best[j - 1]; best[j - 1] = t;
      }
      floor = best[best.length - 1].score;
    }
  }
  if (best.length < k) best.sort((a, b) => b.score - a.score);
  return best;
}
