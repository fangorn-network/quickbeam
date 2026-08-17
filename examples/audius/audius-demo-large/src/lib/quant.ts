// Quantization — the TypeScript half of a cross-language contract.
//
// The Python side is quickbeam/index.py (`int8_encode`, `int8_decode`, `sign_encode`).
// `cdn index --emit-fixture` writes a fixture that check-private.ts asserts this file
// reproduces BYTE FOR BYTE. Byte-exactness is demanded rather than an overlap
// threshold because, unlike `matryoshka`, nothing here has an epsilon to negotiate:
// sign and scalar quantization are scale-invariant integer operations.
//
// Zero imports by design. This file, route.ts and store.ts must stay free of
// graph.ts/types.ts/config.ts so a second consumer is a file move, not a refactor.

/** Round half to EVEN, matching numpy's `rint`.
 *
 *  JS `Math.round` rounds half AWAY FROM ZERO (Math.round(0.5) === 1, and
 *  Math.round(-0.5) === -0), while numpy gives 0 and -0 respectively. Any component
 *  landing exactly on .5 would otherwise quantize to a different byte than the
 *  server holds — a silent one-LSB divergence that no type checker sees. */
export function rint(x: number): number {
  const f = Math.floor(x);
  const d = x - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

/** Symmetric int8, one global scale, no zero-point. Mirrors `index.int8_encode`. */
export function int8Encode(x: Float32Array | number[], scale: number): Int8Array {
  const out = new Int8Array(x.length);
  for (let i = 0; i < x.length; i++) {
    const v = rint((x[i] / scale) * 127);
    out[i] = v > 127 ? 127 : v < -127 ? -127 : v;
  }
  return out;
}

export function int8Decode(codes: Int8Array, scale: number): Float32Array {
  const out = new Float32Array(codes.length);
  const s = scale / 127;
  for (let i = 0; i < codes.length; i++) out[i] = codes[i] * s;
  return out;
}

/** Packed sign bits, MSB-first within each byte — `np.packbits(..., bitorder="big")`.
 *
 *  sign(0) = +1, pinned on both sides. ~0.4% of vectors carry a component within
 *  1e-6 of zero, and an unpinned tie flips one Hamming bit between implementations. */
export function signEncode(x: Float32Array | number[]): Uint8Array {
  const out = new Uint8Array(Math.ceil(x.length / 8));
  for (let i = 0; i < x.length; i++) {
    if (x[i] >= 0) out[i >> 3] |= 0x80 >> (i & 7);
  }
  return out;
}

/** Cosine top-k of a float query against a flat block of int8 document vectors.
 *
 *  The decode scale is a single positive constant, so it cannot change the ORDER —
 *  it is applied once to the returned scores rather than per component, which keeps
 *  the inner loop integer-multiply-accumulate.
 *
 *  Document vectors are unit-norm before quantization, so the dot is the cosine up
 *  to quantization error; that error costs ~4% R@10 against exact fp32, measured. */
export function int8TopK(
  codes: Int8Array,
  count: number,
  dim: number,
  scale: number,
  query: Float32Array,
  k: number,
  accept?: (i: number) => boolean,
): Array<{ index: number; score: number }> {
  const best: Array<{ index: number; score: number }> = [];
  const s = scale / 127;
  let floor = -Infinity;
  for (let i = 0; i < count; i++) {
    if (accept && !accept(i)) continue;
    const off = i * dim;
    let dot = 0;
    for (let d = 0; d < dim; d++) dot += codes[off + d] * query[d];
    dot *= s;
    if (best.length < k) {
      best.push({ index: i, score: dot });
      if (best.length === k) {
        best.sort((a, b) => b.score - a.score);
        floor = best[best.length - 1].score;
      }
    } else if (dot > floor) {
      best[best.length - 1] = { index: i, score: dot };
      for (let j = best.length - 1; j > 0 && best[j].score > best[j - 1].score; j--) {
        const t = best[j]; best[j] = best[j - 1]; best[j - 1] = t;
      }
      floor = best[best.length - 1].score;
    }
  }
  if (best.length < k) best.sort((a, b) => b.score - a.score);
  return best;
}
