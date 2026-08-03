// Does the ported session kernel behave, on this corpus?
//
// The kernel is sonder's, copied nearly verbatim, so what needs testing is not the
// maths — it's the SEAM: a different embedding dimension, a different distance
// metric, and a different field vocabulary. Every one of those can be wrong while
// the code runs happily and just recommends slightly worse things forever.
//
// Usage: quickbeam cdn serve --cdn-dir ./audius-build/cdn --cors --port 8090
//        npm run check:kernel
import assert from 'node:assert/strict';
import { Graph } from '../src/lib/graph.ts';
import { cosToDistance, toFeatures } from '../src/kernel/adapt.ts';
import { emptyKernel, onPlay, onSkip, queryVector } from '../src/kernel/SessionKernel.ts';
import { DEFAULTS, D } from '../src/kernel/constants.ts';
import { dot, l2dist } from '../src/kernel/Vec.ts';

const CDN = process.env.VITE_CDN_URL ?? 'http://localhost:8090';
const DOMAIN = process.env.VITE_DOMAIN ?? 'audius';
const PLATFORM = process.env.VITE_PLATFORM_OWNER ?? '0x1111111111111111111111111111111111111111';

let failures = 0;
async function check(name: string, fn: () => void | Promise<void>) {
  try { await fn(); console.log(`✓ ${name}`); }
  catch (e) { failures++; console.log(`✗ ${name}\n    ${(e as Error).message}`); }
}

const g = new Graph();
console.log(`· loading ${DOMAIN} from ${CDN}`);
const stats = await g.load(CDN, DOMAIN, PLATFORM);
const [platform, artist] = stats.publishers;

// Reach the private vector store the same way the kernel does.
const vecOf = (id: string): Float32Array =>
  (g as unknown as { vectorOf(i: string): Float32Array }).vectorOf(id);

const platformTracks = g.sample('Track', 40, platform.owner);
const artistTracks = g.sample('Track', 40, artist.owner);

await check('dimension matches the corpus', () => {
  assert.equal(D, 256, `kernel D=${D} but this corpus is 256-d`);
  assert.equal(vecOf(platformTracks[0].id).length, 256);
});

// ── 1. the load-bearing conversion ──────────────────────────────────────────
await check('cosine → squared L2 conversion is exact', () => {
  let worst = 0;
  for (let i = 0; i < 30; i++) {
    const a = vecOf(platformTracks[i % platformTracks.length].id);
    const b = vecOf(artistTracks[i % artistTracks.length].id);
    const cos = dot(a, b);
    const direct = l2dist(a, b) ** 2;      // ‖a−b‖², computed the long way
    worst = Math.max(worst, Math.abs(direct - cosToDistance(cos)));
  }
  // Float32 accumulation over 256 dims; anything beyond this means the identity
  // is wrong, not that the arithmetic is noisy.
  assert.ok(worst < 1e-4, `max |‖a−b‖² − (2−2cos)| = ${worst}`);
});

await check('field mapping produces real taste dimensions', () => {
  const feats = platformTracks.slice(0, 25).map((r) => toFeatures(r, vecOf(r.id)));
  assert.ok(feats.every((f) => f.artistId), 'a track has no artistId');
  assert.ok(feats.filter((f) => f.genres.length).length > 15, 'genres mostly empty');
  assert.ok(feats.some((f) => f.moods.length), 'no moods at all');
  assert.ok(feats.some((f) => f.themes.length), 'no themes (tags) at all');
  assert.ok(feats.every((f) => f.durationMs > 0), 'a duration is zero — seconds→ms lost?');
});

// ── 2. state transitions ────────────────────────────────────────────────────
await check('onPlay moves μ toward the played track', () => {
  const t = platformTracks[0];
  const e = vecOf(t.id);
  const s0 = emptyKernel();
  const s1 = onPlay(s0, toFeatures(t, e));
  assert.ok(dot(s1.mu, e) > dot(s0.mu, e), 'μ did not move toward the play');
  assert.equal(s1.t, 1, 'timestep did not advance');
});

await check('velocity becomes non-zero after two plays', () => {
  let s = emptyKernel();
  for (const t of platformTracks.slice(0, 2)) s = onPlay(s, toFeatures(t, vecOf(t.id)));
  const speed = Math.sqrt(dot(s.v, s.v));
  assert.ok(speed > 1e-6, `speed ${speed} — the kernel is not tracking direction`);
});

await check('onSkip pushes μ away and opens a skip region', () => {
  const t = platformTracks[0];
  const e = vecOf(t.id);
  let s = onPlay(emptyKernel(), toFeatures(platformTracks[1], vecOf(platformTracks[1].id)));
  const before = dot(s.mu, e);
  s = onSkip(s, toFeatures(t, e));
  assert.ok(dot(s.mu, e) < before, 'μ did not move away from the skip');
  assert.ok(s.skipCentroid !== null, 'no skip region formed');
  assert.ok(s.entropy > 0.2, 'entropy did not rise on a skip');
});

await check('lookahead saturates rather than running away', () => {
  let s = emptyKernel();
  for (const t of platformTracks.slice(0, 8)) s = onPlay(s, toFeatures(t, vecOf(t.id)));
  const q = queryVector(s);
  const drift = l2dist(q, s.mu);
  assert.ok(drift <= DEFAULTS.lambda_max + 1e-6,
    `lookahead ${drift} exceeded lambda_max ${DEFAULTS.lambda_max}`);
});

// ── 3. skip disambiguation — the branch most likely to break ────────────────
await check('repeated skips of an unplayed artist reach the blacklist', () => {
  // Pick an artist with several tracks so the skips are all genuinely theirs.
  const byArtist = new Map<string, typeof platformTracks>();
  for (const t of g.sample('Track', 600, platform.owner)) {
    const a = String(t.fields.artistId ?? '');
    if (!a) continue;
    byArtist.set(a, [...(byArtist.get(a) ?? []), t]);
  }
  const victim = [...byArtist.values()].find((ts) => ts.length >= 4);
  assert.ok(victim, 'no artist with ≥4 tracks to test suppression');
  let s = emptyKernel();
  for (const t of victim!.slice(0, 4)) s = onSkip(s, toFeatures(t, vecOf(t.id)));
  const a = String(victim![0].fields.artistId);
  assert.ok(s.blacklist.has(a),
    `neg=${s.neg[a]} did not pass theta_B=${DEFAULTS.theta_B}`);
});

await check('skipping a PLAYED artist mutes for fatigue instead of blacklisting', () => {
  const byArtist = new Map<string, typeof platformTracks>();
  for (const t of g.sample('Track', 600, platform.owner)) {
    const a = String(t.fields.artistId ?? '');
    if (!a) continue;
    byArtist.set(a, [...(byArtist.get(a) ?? []), t]);
  }
  const pair = [...byArtist.values()].find((ts) => ts.length >= 2);
  assert.ok(pair, 'no artist with ≥2 tracks');
  const a = String(pair![0].fields.artistId);
  let s = onPlay(emptyKernel(), toFeatures(pair![0], vecOf(pair![0].id)));
  assert.ok(s.artists[a] > DEFAULTS.fatigue_threshold,
    `one play left EMA ${s.artists[a]} below fatigue_threshold`);
  s = onSkip(s, toFeatures(pair![1], vecOf(pair![1].id)));
  assert.ok(s.muted.has(a), 'a fatigued artist was not muted');
  assert.ok(!s.blacklist.has(a), 'fatigue wrongly escalated to a blacklist');
  assert.equal(s.neg[a] ?? 0, 0, 'fatigue incremented the dislike accumulator');
});

// ── 4. the kernel actually drives recommendations ───────────────────────────
await check('cold kernel recommends nothing', () => {
  g.kernelReset();
  assert.equal(g.kernelRecommend(10).length, 0, 'recommended without any signal');
});

await check('one play produces recommendations', () => {
  g.kernelReset();
  g.kernelSignal(platformTracks[0].id, 'play');
  const recs = g.kernelRecommend(10);
  assert.ok(recs.length > 0, 'no recommendations after a play');
  assert.ok(!recs.some((r) => r.id === platformTracks[0].id), 'recommended the seed back');
});

await check('recommendations move when the signal moves', () => {
  g.kernelReset();
  g.kernelSignal(platformTracks[0].id, 'play');
  const before = g.kernelRecommend(10).map((r) => r.id);
  for (const t of artistTracks.slice(0, 3)) g.kernelSignal(t.id, 'like');
  const after = g.kernelRecommend(10).map((r) => r.id);
  const overlap = after.filter((id) => before.includes(id)).length;
  assert.ok(overlap < before.length,
    'recommendations identical after 3 new signals — the kernel is inert');
});

await check('a disliked neighbourhood recedes', () => {
  g.kernelReset();
  const seed = platformTracks[0];
  g.kernelSignal(seed.id, 'play');
  const ranked = g.kernelRecommend(30);
  const target = ranked[0];
  g.kernelSignal(target.id, 'dislike');
  const after = g.kernelRecommend(30);
  assert.ok(!after.some((r) => r.id === target.id), 'disliked record came back');
  // Its nearest neighbours should also lose ground, not just the record itself.
  const tv = vecOf(target.id);
  const meanCos = (rs: typeof ranked) =>
    rs.reduce((s, r) => s + dot(vecOf(r.id), tv), 0) / Math.max(rs.length, 1);
  assert.ok(meanCos(after) < meanCos(ranked),
    'the region around the dislike did not recede');
});

// ── 5. the claim the demo makes ─────────────────────────────────────────────
await check('the publisher boundary is not a fence (crossing appears with depth)', () => {
  // Measured, not assumed: seeded from the artist, the top ~20 are all theirs —
  // `tau_art` correctly favours the artist you just played, and they have ~38
  // unplayed tracks. Crossing begins once that neighbourhood thins out. Asserting
  // crossing at k=12 would be asserting a WORSE recommender.
  g.kernelReset();
  for (const t of artistTracks.slice(0, 3)) g.kernelSignal(t.id, 'play');
  const deep = g.kernelRecommend(60);
  const crossed = deep.filter((r) => (r.owner ?? '').toLowerCase() !== artist.owner.toLowerCase());
  assert.ok(deep.length > 20, `only ${deep.length} recommendations at k=60`);
  assert.ok(crossed.length > 0,
    'no recommendation from the other publisher even at depth 60 — the kernel is fenced');
  console.log(`    k=60: ${crossed.length}/${deep.length} from the other publisher`);
});

await check('the same ranking, filtered to the other publisher, is non-empty', () => {
  // This is what the "from the other repo" rail renders: the identical weights,
  // narrowed — so it shows real cross-publisher picks at a rail-sized k without
  // re-ranking or otherwise flattering the demo.
  g.kernelReset();
  for (const t of artistTracks.slice(0, 3)) g.kernelSignal(t.id, 'play');
  const other = g.kernelRecommend(8, platform.owner);
  assert.ok(other.length > 0, 'filtered rail would render empty');
  assert.ok(other.every((r) => (r.owner ?? '').toLowerCase() === platform.owner.toLowerCase()),
    'owner filter leaked records from the wrong publisher');
  console.log(`    e.g. ${other.slice(0, 3).map((r) => JSON.stringify(String(r.fields.title).slice(0, 28))).join(', ')}`);
});

await check('the snapshot reports something a readout can render', () => {
  g.kernelReset();
  for (const t of platformTracks.slice(0, 4)) g.kernelSignal(t.id, 'play');
  const snap = g.kernelSnapshot();
  assert.ok(snap.timestep === 4, `timestep ${snap.timestep} after 4 plays`);
  assert.ok(snap.topGenres.length > 0, 'no genre taste accumulated');
  assert.ok(snap.spread > 0 && snap.entropy > 0, 'spread/entropy unset');
  console.log(`    speed=${snap.speed} spread=${snap.spread} entropy=${snap.entropy} ` +
    `top=${snap.topGenres.slice(0, 2).map(([k]) => k).join('/')}`);
});

if (failures) { console.error(`\n${failures} kernel check(s) failed`); process.exit(1); }
console.log('\nSession kernel behaves on this corpus.');
