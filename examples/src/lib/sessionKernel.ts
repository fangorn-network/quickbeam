// sessionKernel — an ambient model of "where this browsing session is heading."
//
// This is the un-Gemini-able part of the app: a geometric (not generative) model
// that lives in the browser, runs over the PRIVATE embedded corpus, and reacts to
// the user's own like/dislike signal in real time. It never asks a question; it
// learns the shape of the outing and projects it forward, then surfaces a "where
// you're heading" rail (see HeadingRail).
//
// Adapted from the music sessionKernel (a streaming Markov kernel over a play/skip
// stream). Here the signal is an EXPLICIT like/dislike SET rather than a noisy
// behavioral stream, so the state is a pure recompute from the two lists — simpler
// and order-robust, while keeping the load-bearing ideas:
//
//   μ  — session centroid: a recency-weighted mean of liked vectors (where you are)
//   v  — velocity: recent-likes mean minus older-likes mean (where you're heading)
//   q  — query vector: μ nudged along v̂ (a small lookahead in the direction of drift)
//   taste — signed tag affinity (+liked / −disliked) reweighting candidates
//   skip region — disliked vectors form a repulsion field (centroid + radius)
//
// All vector ops are plain number[] in the corpus's raw embedding space; ranking is
// by cosine, so only direction matters (we normalise where it counts).

// ---- tiny vector helpers (corpus space, plain arrays) ----
type V = number[];

function zeros(d: number): V {
  return new Array<number>(d).fill(0);
}
function add(a: V, b: V): V {
  const out = new Array<number>(a.length);
  for (let i = 0; i < a.length; i++) out[i] = a[i] + b[i];
  return out;
}
function sub(a: V, b: V): V {
  const out = new Array<number>(a.length);
  for (let i = 0; i < a.length; i++) out[i] = a[i] - b[i];
  return out;
}
function scale(a: V, k: number): V {
  const out = new Array<number>(a.length);
  for (let i = 0; i < a.length; i++) out[i] = a[i] * k;
  return out;
}
function norm(a: V): number {
  let s = 0;
  for (const x of a) s += x * x;
  return Math.sqrt(s);
}
function l2dist(a: V, b: V): number {
  let s = 0;
  for (let i = 0; i < a.length; i++) {
    const d = a[i] - b[i];
    s += d * d;
  }
  return Math.sqrt(s);
}
function mean(vs: V[]): V {
  const d = vs[0].length;
  const out = zeros(d);
  for (const v of vs) for (let i = 0; i < d; i++) out[i] += v[i];
  return scale(out, 1 / vs.length);
}
// Recency-weighted mean — newest entries (end of the list) weigh most.
function recencyMean(vs: V[]): V {
  const n = vs.length;
  const d = vs[0].length;
  const out = zeros(d);
  let wsum = 0;
  vs.forEach((v, i) => {
    const w = 1 / Math.log(n - i + 1); // i=n-1 (newest) → largest weight
    for (let c = 0; c < d; c++) out[c] += v[c] * w;
    wsum += w;
  });
  return scale(out, 1 / (wsum || 1));
}

// ---- inputs / state ----
// A resolved signal: the corpus vector + the tags we reweight taste on. The hook
// turns a liked/disliked id into this via the Atlas model (which holds the vectors).
export interface Signal {
  vector: V;
  tags: string[];
}

export interface SessionState {
  mu: V | null; // null until there's at least one like
  v: V;
  speed: number;
  taste: Record<string, number>; // signed: +liked, −disliked
  topTags: string[]; // strongest positive tags, for the rail subtitle / narration
  skipCentroid: V | null;
  skipRadius: number;
}

// Tags an entity contributes to taste: its type + categories/primaryType. Kept here
// (not in the data layer) so the kernel owns its own notion of "taste dimensions."
export function entityTags(type: string, fields: Record<string, unknown>): string[] {
  const out = new Set<string>();
  if (type) out.add(type.toLowerCase());
  const f = fields;
  const cats = Array.isArray(f.categories) ? f.categories.map(String) : [];
  for (const c of cats.slice(0, 4)) out.add(c.replace(/_/g, ' ').toLowerCase());
  if (typeof f.primaryType === 'string' && f.primaryType) out.add(f.primaryType.replace(/_/g, ' ').toLowerCase());
  return [...out];
}

const LAMBDA_MAX = 0.4; // lookahead cap, as a fraction of μ̂ (direction-space)
const DISLIKE_TASTE = 0.6; // a disliked tag's negative pull vs. a liked tag's +1

// Build the session state from the ordered like/dislike signal sets. Pure.
export function buildSession(likes: Signal[], dislikes: Signal[]): SessionState {
  const empty: SessionState = { mu: null, v: [], speed: 0, taste: {}, topTags: [], skipCentroid: null, skipRadius: 0 };
  if (!likes.length) return empty;

  const likedVecs = likes.map((l) => l.vector);
  const mu = recencyMean(likedVecs);

  // Velocity: where recent likes sit relative to earlier ones. Needs ≥2 likes.
  let v = zeros(mu.length);
  if (likes.length >= 2) {
    const half = Math.floor(likes.length / 2);
    const older = mean(likedVecs.slice(0, Math.max(1, half)));
    const newer = mean(likedVecs.slice(half));
    v = sub(newer, older);
  }

  // Taste: +1 per liked tag (recency-weighted), −DISLIKE_TASTE per disliked tag.
  const taste: Record<string, number> = {};
  likes.forEach((l, i) => {
    const w = 1 / Math.log(likes.length - i + 1);
    for (const t of l.tags) taste[t] = (taste[t] ?? 0) + w;
  });
  for (const dlk of dislikes) for (const t of dlk.tags) taste[t] = (taste[t] ?? 0) - DISLIKE_TASTE;

  const topTags = Object.entries(taste)
    .filter(([, w]) => w > 0)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 3)
    .map(([t]) => t);

  // Skip region: disliked vectors as a repulsion field around their centroid.
  let skipCentroid: V | null = null;
  let skipRadius = 0;
  if (dislikes.length) {
    skipCentroid = mean(dislikes.map((d) => d.vector));
    for (const d of dislikes) skipRadius = Math.max(skipRadius, l2dist(d.vector, skipCentroid));
  }

  return { mu, v, speed: norm(v), taste, topTags, skipCentroid, skipRadius };
}

// The vector to rank the corpus by: μ nudged along the heading v̂. Direction-space,
// so we normalise μ and v and blend (cosine ranking ignores magnitude anyway).
export function queryVector(state: SessionState): V | null {
  if (!state.mu) return null;
  const mn = norm(state.mu) || 1;
  const muHat = scale(state.mu, 1 / mn);
  if (state.speed < 1e-9) return muHat;
  const vHat = scale(state.v, 1 / state.speed);
  // Lookahead grows with how decisively recent likes diverge from earlier ones.
  const lambda = LAMBDA_MAX * Math.tanh((2 * state.speed) / mn);
  return add(muHat, scale(vHat, lambda));
}

// ---- reranking candidates ----
// A candidate the data layer hands us: the corpus point plus its raw vector and the
// base cosine to the query vector. The kernel folds in taste + skip repulsion.
export interface Candidate {
  id: string;
  type: string;
  title: string;
  fields: Record<string, unknown>;
  vector: V;
  baseScore: number; // cosine(queryVector, vector)
}

export interface RankedCandidate extends Candidate {
  score: number; // post-reweight, for ordering + the rail's "match" meter
}

const TAU_TASTE = 0.15; // taste's pull on the final score
const GAMMA_SKIP = 0.5; // disliked-region repulsion strength

function tasteAffinity(taste: Record<string, number>, tags: string[]): number {
  if (!tags.length) return 0;
  let s = 0;
  for (const t of tags) s += taste[t] ?? 0;
  return s / tags.length; // signed average; >0 boosts, <0 penalises
}

// Reweight + sort candidates by the session: base similarity, nudged by taste and
// pushed down inside the disliked region. Returns the strongest `k`.
export function rankBySession(candidates: Candidate[], state: SessionState, k: number): RankedCandidate[] {
  const ranked = candidates.map((c) => {
    let s = c.baseScore + TAU_TASTE * tasteAffinity(state.taste, entityTags(c.type, c.fields));
    if (state.skipCentroid && state.skipRadius > 1e-6) {
      const dc = l2dist(c.vector, state.skipCentroid);
      s -= GAMMA_SKIP * Math.exp(-(dc * dc) / (2 * state.skipRadius * state.skipRadius));
    }
    return { ...c, score: s };
  });
  ranked.sort((a, b) => b.score - a.score);
  return ranked.slice(0, k);
}
