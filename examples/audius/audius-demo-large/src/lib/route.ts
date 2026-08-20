// Local routing against the public codebook.
//
// This is the file that makes the privacy claim true: the query vector is compared
// against the codebook HERE, in the tab, and the only thing that survives the call is
// an integer bucket id. Nothing in this module touches the network — see store.ts,
// which is the one place that does.
//
// Zero imports by design (see quant.ts).

export interface Layout {
  k: number;
  dim: number;
  nbuckets: number;
  salt: string;
  /** cell -> bucket. Public, baked, identical for every client. */
  buckets: number[];
  cell_sizes: number[];
  codebook_scale: number;
  vector_scale: number;
  count: number;
}

/** Index of the nearest centroid to `q`.
 *
 *  Scored against the RAW int8 codebook: decoding would multiply every centroid by
 *  one positive constant, which cannot change an argmax. (The centroids were
 *  unit-norm before quantization and are very nearly so after, so this is a cosine.)
 *
 *  Note the client's cell can differ from the one Python's float k-means would pick
 *  for a query sitting almost exactly between two centroids — int8 rounding moves the
 *  boundary by a hair. That is harmless: there is no "correct" cell to match, only a
 *  good one, and check-private asserts the two agree on real queries anyway. */
export function nearestCell(codebook: Int8Array, layout: Layout, q: Float32Array): number {
  const { k, dim } = layout;
  let bestI = 0;
  let bestS = -Infinity;
  for (let i = 0; i < k; i++) {
    const off = i * dim;
    let dot = 0;
    for (let d = 0; d < dim; d++) dot += codebook[off + d] * q[d];
    if (dot > bestS) { bestS = dot; bestI = i; }
  }
  return bestI;
}

/** The `nprobe` nearest cells, best first. Raising nprobe buys recall linearly in
 *  bytes; it does NOT widen what is disclosed beyond the buckets those cells fall
 *  into, which is why it is the knob to reach for before touching bucket size. */
export function nearestCells(
  codebook: Int8Array, layout: Layout, q: Float32Array, nprobe: number,
): number[] {
  const { k, dim } = layout;
  const scored: Array<{ i: number; s: number }> = [];
  for (let i = 0; i < k; i++) {
    const off = i * dim;
    let dot = 0;
    for (let d = 0; d < dim; d++) dot += codebook[off + d] * q[d];
    scored.push({ i, s: dot });
  }
  scored.sort((a, b) => b.s - a.s);
  return scored.slice(0, Math.max(1, nprobe)).map((x) => x.i);
}

export function bucketOf(layout: Layout, cell: number): number {
  return layout.buckets[cell];
}

/** The buckets to request for a query — deduped, because two probed cells sharing a
 *  bucket are ONE request. That dedupe is also what makes a themed session cheap:
 *  nearby cells collide into the same bucket and the second query sends nothing. */
export function bucketsFor(
  codebook: Int8Array, layout: Layout, q: Float32Array, nprobe = 1,
): number[] {
  const cells = nearestCells(codebook, layout, q, nprobe);
  return [...new Set(cells.map((c) => layout.buckets[c]))];
}
