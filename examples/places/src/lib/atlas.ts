// Atlas model — the semantic map behind the /atlas screen.
//
// Every document already lives in the browser as a 256-d vector (shards mode) or a
// toy vector (mock mode). This module turns that high-d cloud into 2-D screen
// positions and answers "where does this query land, and what's around it?".
//
// Projection: if the served snapshot carries a baked `proj` (UMAP, computed by
// `quickbeam cdn bake`), we use it verbatim — that's the high-quality, cluster-
// separated layout. Otherwise we compute a PCA projection client-side from the
// vectors themselves. PCA is linear (less cluster-separated than UMAP) but real,
// deterministic, and fast enough for a few thousand points with no dependency.
//
// Query: embed the typed query in the SAME space (real model in shards mode, the
// toy token embedder in mock mode), cosine-rank the documents, and place the query
// marker at the score-weighted centroid of its nearest neighbors. We do NOT try to
// run the query vector through UMAP/PCA directly — the neighbor centroid is cheap
// and honest about what it shows ("your words landed amid these places").
import { IS_MOCK, IS_SHARDS } from './config';
import { mockAtlasRaw, mockAtlasEmbed } from './mock';
import { shardAtlasRaw, shardAtlasEmbed } from './shards';
import type { AtlasRaw } from './atlasTypes';

export interface AtlasPoint {
  id: string;
  type: string;
  title: string;
  x: number; // normalized to [0,1] — semantic (UMAP/PCA) coord
  y: number; // normalized to [0,1] — semantic (UMAP/PCA) coord
  fields: Record<string, unknown>;
}

export interface AtlasNeighbor {
  id: string;
  score: number;
}

export interface AtlasQueryResult {
  neighbors: AtlasNeighbor[];
  marker: { x: number; y: number };
}

// A corpus point with its raw vector — the substrate the session kernel ranks over.
export interface AtlasVectorPoint {
  id: string;
  type: string;
  title: string;
  vector: number[];
  fields: Record<string, unknown>;
}

export interface AtlasModel {
  points: AtlasPoint[];
  projection: 'umap' | 'pca';
  // Embed + rank a typed query; returns neighbors (desc by cosine) and a marker.
  query(q: string, k?: number): Promise<AtlasQueryResult>;
  // Map a point id to its 2-D position (for highlighting / fly-to).
  position(id: string): { x: number; y: number } | undefined;
  // Resolve a point's raw vector + fields (for the session kernel's like/dislike set).
  vectorPoint(id: string): AtlasVectorPoint | undefined;
  // Rank the whole corpus by cosine to an arbitrary vector (the session query). Returns
  // the top `pool` as candidates (with their base cosine) the kernel reweights;
  // `exclude` drops already-rated ids.
  rankByVector(qv: number[], pool: number, exclude?: Set<string>): Array<AtlasVectorPoint & { baseScore: number }>;
}

// ---- raw access (per data source) ----
async function rawPoints(): Promise<AtlasRaw[]> {
  if (IS_SHARDS) return shardAtlasRaw();
  if (IS_MOCK) return mockAtlasRaw();
  // qdrant mode keeps vectors server-side; the Atlas needs them in the browser.
  return [];
}

async function embed(q: string): Promise<number[]> {
  if (IS_MOCK) return mockAtlasEmbed(q);
  return shardAtlasEmbed(q);
}

// ---- PCA (power iteration, no D×D covariance) ----
// X: N×D mean-centered. Returns the top-2 principal axes (each length D).
function pca2(X: number[][], iters = 40): [number[], number[]] {
  const n = X.length;
  const d = X[0]?.length ?? 0;
  if (!n || !d) return [[], []];

  const randUnit = (seed: number): number[] => {
    // Deterministic pseudo-random unit vector so the layout is stable per load.
    let s = seed >>> 0;
    const v = new Array<number>(d);
    let norm = 0;
    for (let i = 0; i < d; i++) {
      s = (s * 1664525 + 1013904223) >>> 0;
      v[i] = s / 0xffffffff - 0.5;
      norm += v[i] * v[i];
    }
    norm = Math.sqrt(norm) || 1;
    for (let i = 0; i < d; i++) v[i] /= norm;
    return v;
  };

  // One power-iteration step: v ← normalize(Xᵀ(X v)). Optionally orthogonalize
  // against an already-found axis each step (Gram–Schmidt) to get the 2nd component.
  const powerIter = (against?: number[]): number[] => {
    let v = randUnit(against ? 99 : 7);
    for (let it = 0; it < iters; it++) {
      const proj = new Array<number>(n); // X v  (length N)
      for (let r = 0; r < n; r++) {
        const row = X[r];
        let s = 0;
        for (let c = 0; c < d; c++) s += row[c] * v[c];
        proj[r] = s;
      }
      const nv = new Array<number>(d).fill(0); // Xᵀ proj  (length D)
      for (let r = 0; r < n; r++) {
        const row = X[r];
        const p = proj[r];
        for (let c = 0; c < d; c++) nv[c] += row[c] * p;
      }
      if (against) {
        let dot = 0;
        for (let c = 0; c < d; c++) dot += nv[c] * against[c];
        for (let c = 0; c < d; c++) nv[c] -= dot * against[c];
      }
      let norm = 0;
      for (let c = 0; c < d; c++) norm += nv[c] * nv[c];
      norm = Math.sqrt(norm) || 1;
      for (let c = 0; c < d; c++) nv[c] /= norm;
      v = nv;
    }
    return v;
  };

  const a1 = powerIter();
  const a2 = powerIter(a1);
  return [a1, a2];
}

// Normalize a list of (x,y) into [0,1] with a small margin, preserving aspect.
function normalizeCoords(xy: Array<[number, number]>): Array<[number, number]> {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const [x, y] of xy) {
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  const span = Math.max(maxX - minX, maxY - minY) || 1;
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const m = 0.06; // margin
  return xy.map(([x, y]) => [
    0.5 + ((x - cx) / span) * (1 - 2 * m),
    0.5 + ((y - cy) / span) * (1 - 2 * m),
  ]);
}

// Build the projected coordinates for every raw point.
function project(raw: AtlasRaw[]): { coords: Array<[number, number]>; mode: 'umap' | 'pca' } {
  const haveBaked = raw.length > 0 && raw.every((r) => r.proj);
  if (haveBaked) {
    return { coords: normalizeCoords(raw.map((r) => r.proj as [number, number])), mode: 'umap' };
  }
  // PCA fallback. Mean-center (subsample the matrix used to *find* axes; project all).
  const d = raw[0]?.vector.length ?? 0;
  const mean = new Array<number>(d).fill(0);
  for (const r of raw) for (let c = 0; c < d; c++) mean[c] += r.vector[c];
  for (let c = 0; c < d; c++) mean[c] /= raw.length || 1;

  const MAX_FIT = 2500;
  const stride = Math.max(1, Math.floor(raw.length / MAX_FIT));
  const fit: number[][] = [];
  for (let i = 0; i < raw.length; i += stride) {
    const row = new Array<number>(d);
    for (let c = 0; c < d; c++) row[c] = raw[i].vector[c] - mean[c];
    fit.push(row);
  }
  const [a1, a2] = pca2(fit);

  const coords: Array<[number, number]> = raw.map((r) => {
    let x = 0, y = 0;
    for (let c = 0; c < d; c++) {
      const v = r.vector[c] - mean[c];
      x += v * a1[c];
      y += v * a2[c];
    }
    return [x, y];
  });
  return { coords: normalizeCoords(coords), mode: 'pca' };
}

function cosine(a: number[], an: number, b: number[], bn: number): number {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s / (an * bn);
}

let _model: Promise<AtlasModel> | null = null;

export async function loadAtlasModel(): Promise<AtlasModel> {
  return (_model ??= (async () => {
    const raw = await rawPoints();
    if (!raw.length) {
      throw new Error('Atlas needs in-browser vectors (run the app in mock or shards mode).');
    }
    const { coords, mode } = project(raw);
    const points: AtlasPoint[] = raw.map((r, i) => ({
      id: r.id,
      type: r.type,
      title: r.title,
      x: coords[i][0],
      y: coords[i][1],
      fields: r.fields,
    }));
    const posById = new Map<string, { x: number; y: number }>();
    points.forEach((p) => posById.set(p.id, { x: p.x, y: p.y }));

    // Precompute norms once for query cosine.
    const norms = raw.map((r) => {
      let s = 0;
      for (const v of r.vector) s += v * v;
      return Math.sqrt(s) || 1;
    });

    const idxById = new Map<string, number>();
    raw.forEach((r, i) => idxById.set(r.id, i));
    const toVectorPoint = (r: AtlasRaw): AtlasVectorPoint => ({
      id: r.id,
      type: r.type,
      title: r.title,
      vector: r.vector,
      fields: r.fields,
    });

    return {
      points,
      projection: mode,
      position: (id) => posById.get(id),
      vectorPoint(id) {
        const i = idxById.get(id);
        return i === undefined ? undefined : toVectorPoint(raw[i]);
      },
      rankByVector(qv, pool, exclude) {
        let qn = 0;
        for (const v of qv) qn += v * v;
        qn = Math.sqrt(qn) || 1;
        const scored: Array<AtlasVectorPoint & { baseScore: number }> = [];
        for (let i = 0; i < raw.length; i++) {
          if (exclude?.has(raw[i].id)) continue;
          scored.push({ ...toVectorPoint(raw[i]), baseScore: cosine(raw[i].vector, norms[i], qv, qn) });
        }
        scored.sort((a, b) => b.baseScore - a.baseScore);
        return scored.slice(0, pool);
      },
      async query(q, k = 12) {
        const qv = await embed(q);
        let qn = 0;
        for (const v of qv) qn += v * v;
        qn = Math.sqrt(qn) || 1;
        const scored = raw.map((r, i) => ({ id: r.id, score: cosine(r.vector, norms[i], qv, qn) }));
        scored.sort((a, b) => b.score - a.score);
        const neighbors = scored.slice(0, k);
        // Score-weighted centroid of the top neighbors → marker position. Shift the
        // (cosine) weights so even modest scores contribute, but the best dominate.
        let wx = 0, wy = 0, wsum = 0;
        for (const nbr of neighbors) {
          const pos = posById.get(nbr.id);
          if (!pos) continue;
          const w = Math.max(0, nbr.score) ** 2 + 1e-3;
          wx += pos.x * w;
          wy += pos.y * w;
          wsum += w;
        }
        const marker = wsum ? { x: wx / wsum, y: wy / wsum } : { x: 0.5, y: 0.5 };
        return { neighbors, marker };
      },
    };
  })());
}
