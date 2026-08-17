// A toy embedding space for the mock document vectors.
//
// Each token (genre, mood, title word, …) maps deterministically to a fixed random
// unit-ish vector; a "document" is embedded as the normalized sum of its token
// vectors. This is a bag-of-words random projection: token overlap → high cosine. It
// is NOT semantically real, but it gives the served mock vectors enough structure
// that `recommend` ("similar entries") returns thematically related items — exactly
// what real served embeddings will do, just synthetic. When real CDN shards arrive,
// document vectors come straight from the shards and this file is no longer used.
//
// There is no query embedding here: the browser app does keyword search, not
// free-text semantic search, so nothing embeds the user's typed query.
export const MOCK_DIM = 256;

function hashStr(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function mulberry32(seed: number): () => number {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const baseCache = new Map<string, number[]>();
function baseFor(token: string): number[] {
  let v = baseCache.get(token);
  if (!v) {
    const r = mulberry32(hashStr(token));
    v = Array.from({ length: MOCK_DIM }, () => r() * 2 - 1);
    baseCache.set(token, v);
  }
  return v;
}

function normalize(v: number[]): number[] {
  const n = Math.sqrt(v.reduce((s, x) => s + x * x, 0)) || 1;
  return v.map((x) => x / n);
}

export function embedTokens(tokens: string[]): number[] {
  const acc = new Array(MOCK_DIM).fill(0);
  let any = false;
  for (const raw of tokens) {
    const t = raw.toLowerCase().trim();
    if (t.length < 3) continue;
    const b = baseFor(t);
    for (let i = 0; i < MOCK_DIM; i++) acc[i] += b[i];
    any = true;
  }
  if (!any) acc[0] = 1; // avoid an all-zero vector
  return normalize(acc);
}

export function cosine(a: number[], b: number[]): number {
  let d = 0;
  for (let i = 0; i < a.length; i++) d += a[i] * b[i];
  return d; // both pre-normalized
}
