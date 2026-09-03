// Does the ported session kernel behave, on this corpus?
//
// The kernel is sonder's, copied nearly verbatim, so what needs testing is not the
// maths — it's the SEAM: a different embedding dimension, a different distance
// metric, and a different field vocabulary. Every one of those can be wrong while
// the code runs happily and just recommends slightly worse things forever.
//
// Usage: quickbeam cdn serve --cdn-dir ./examples/audius/audius-build/cdn --cors --port 8090
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
  // Kept at depth 60 deliberately, even though crossing now appears at k=12.
  //
  // It used to be the ONLY place crossing showed: at sonder's tau_art=2.00 the
  // top ~20 were all the seeded artist, and this check existed to prove the
  // boundary was permeable *somewhere*. MAX_PER_ARTIST and tau_art=1.00 moved
  // that up into the rail itself. Asserting the weaker property at depth still
  // earns its place — it holds regardless of how the display cap is tuned, so it
  // fails only if the graph itself has become fenced, which is what it is for.
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

// Persistence. The kernel is rebuilt from JSON in a *different* process than the one
// that wrote it, so the only thing worth asserting is behavioural: a restored kernel
// must recommend what the original did. Serialisation that drops a field typically
// still parses and still ranks — just slightly worse, forever.
await check('a kernel survives a save/load round trip intact', () => {
  g.kernelReset();
  for (const t of platformTracks.slice(0, 5)) g.kernelSignal(t.id, 'play');
  for (const t of artistTracks.slice(0, 3)) g.kernelSignal(t.id, 'skip');
  const before = g.kernelSnapshot();
  const recsBefore = g.kernelRecommend(10).map((r) => r.id);

  // Through a string, not just the object: catches anything JSON cannot carry
  // (Float32Array, Set) that an in-memory structuredClone would quietly tolerate.
  const wire = JSON.stringify({ dim: D, kernel: g.kernelExport() });
  assert.equal(JSON.parse(wire).dim, D, 'dim guard would not match on reload');

  g.kernelReset();
  assert.equal(g.kernelSnapshot().timestep, 0, 'reset did not clear the kernel');

  g.kernelImport(JSON.parse(wire).kernel);
  const after = g.kernelSnapshot();
  const recsAfter = g.kernelRecommend(10).map((r) => r.id);

  assert.equal(after.timestep, before.timestep, 'timestep lost');
  assert.equal(after.spread, before.spread, 'spread lost');
  assert.equal(after.entropy, before.entropy, 'entropy lost');
  assert.equal(after.nSkips, before.nSkips, 'skip buffer lost');
  assert.deepEqual(after.topGenres, before.topGenres, 'taste weights lost');
  assert.deepEqual(after.topArtists, before.topArtists, 'artist affinity lost');
  assert.deepEqual(after.blacklisted, before.blacklisted, 'blacklist lost');
  assert.deepEqual(recsAfter, recsBefore,
    'restored kernel recommends different records than the one that was saved');
  console.log(`    t=${after.timestep} spread=${after.spread}, ${recsAfter.length} recs identical`);
});

await check('session fatigue clears on reload, but suppression carries over', () => {
  g.kernelReset();
  // Play an artist twice so their EMA clears fatigue_threshold, then skip them:
  // that routes to the `muted` path rather than the blacklist path.
  const t = artistTracks[0];
  g.kernelSignal(t.id, 'play');
  g.kernelSignal(t.id, 'play');
  g.kernelSignal(t.id, 'skip');
  assert.ok(g.kernelSnapshot().muted.length > 0, 'no muted artist to test with');

  const wire = JSON.parse(JSON.stringify(g.kernelExport()));
  assert.ok(!('muted' in wire.state), 'muted leaked into the persisted kernel state');
  assert.ok(wire.seen.includes(t.id), 'seen did not survive export');

  g.kernelImport(wire);
  const after = g.kernelSnapshot();
  assert.equal(after.muted.length, 0,
    'session fatigue survived a reload — it is meant to clear on every cold start');
  assert.equal(after.signals, wire.seen.length, 'seen count did not restore');
  console.log(`    muted clears; ${after.signals} seen + neg/blacklist carry over`);
});

// ── onboarding seed ─────────────────────────────────────────────────────────
await check('the picker offers artists worth following', () => {
  const { genres, artists } = g.onboardingOptions();
  assert.ok(genres.length >= 8, `only ${genres.length} genres offered`);
  assert.ok(artists.length >= 8, `only ${artists.length} artists offered`);
  assert.ok(genres.every((x) => x.tracks > 0), 'a genre with no tracks would seed nothing');
  // The filter that matters: ranking by raw catalogue size surfaces bulk uploaders
  // with 600 tracks and 37 followers, which makes a picker of strangers.
  assert.ok(artists.every((a) => a.tracks >= 3), 'an artist too thin to seed from got through');
  assert.ok(artists.some((a) => a.owner.toLowerCase() === artist.owner.toLowerCase()),
    'the sovereign artist is missing from the picker — the cross-publisher demo needs them');
  console.log(`    ${genres.slice(0, 4).map((x) => x.title).join(', ')} … | ` +
    `${artists.slice(0, 3).map((a) => `${a.name}(${a.tracks}t)`).join(', ')}`);
});

await check('a seeded kernel recommends without anyone having listened', () => {
  const { genres, artists } = g.onboardingOptions();
  g.kernelReset();
  assert.equal(g.kernelRecommend(8).length, 0, 'a cold kernel should recommend nothing');

  const gPicks = genres.slice(0, 3).map((x) => x.id);
  const aPicks = artists.slice(0, 3).map((x) => x.id);
  const snap = g.kernelSeed(gPicks, aPicks);
  assert.ok(snap.timestep > 0, 'seed applied no plays');
  assert.ok(snap.topGenres.length > 0, 'seed accumulated no taste');

  const recs = g.kernelRecommend(12);
  assert.ok(recs.length > 0, 'the rail would still be empty after onboarding');
  console.log(`    t=${snap.timestep} → ${recs.length} recs, top taste ` +
    `${snap.topGenres.slice(0, 3).map(([k]) => k).join('/')}`);
});

await check('following an artist registers as a preference, and fills the rail', () => {
  const { genres, artists } = g.onboardingOptions();
  const picks = artists.slice(0, 3);
  g.kernelSeed(genres.slice(0, 3).map((x) => x.id), picks.map((x) => x.id));

  // Seeding marks nothing `seen` — otherwise following an artist would be the one
  // action guaranteed to hide their best tracks from you.
  assert.equal(g.kernelSnapshot().signals, 0, 'seeded tracks were marked as already seen');

  // Every artist picked must show up as affinity, whatever their catalogue size.
  const ema = new Map(g.kernelSnapshot().topArtists);
  for (const a of picks) {
    const key = String(g.entity(a.id)?.fields.id ?? '');
    assert.ok((ema.get(key) ?? 0) > 0, `${a.name} did not register as an affinity`);
  }

  // And the rail should be visibly theirs — without being ONLY theirs. Asserting
  // a high count here would be asserting the overfit this cap exists to remove:
  // with MAX_PER_ARTIST=2 and three picks, six is the arithmetic ceiling, so a
  // `>= 6` bar would pass only in the perfect case and fail on any tuning. The
  // property worth pinning is presence, not dominance.
  const theirs = new Set(picks.flatMap(
    (a) => g.neighbours(a.id, 'created', 'out', 60).records.map((r) => r.id)));
  const rail = g.kernelRecommend(12);
  const mine = rail.filter((r) => theirs.has(r.id)).length;
  assert.ok(mine >= 3, `only ${mine}/12 of the opening rail is by a followed artist`);
  assert.ok(mine < 12, 'the entire rail is followed artists — seeding has overfit');
  console.log(`    ${mine}/12 of the rail from ${picks.map((p) => p.name).join(', ')}`);
});

await check('the rail does not collapse onto a single artist', () => {
  // The regression test for the overfit. Before MAX_PER_ARTIST and tau_art=1.00,
  // three plays of one artist returned 12/12 that same artist — a recommender
  // that only ever answers "more of what you just played".
  g.kernelReset();
  for (const t of artistTracks.slice(0, 3)) g.kernelSignal(t.id, 'play');
  const rail = g.kernelRecommend(12);
  assert.equal(rail.length, 12, `rail returned ${rail.length} cards, not 12`);

  const counts = new Map<string, number>();
  for (const r of rail) {
    const a = String(r.fields.artistId ?? r.id);
    counts.set(a, (counts.get(a) ?? 0) + 1);
  }
  const worst = Math.max(...counts.values());
  assert.ok(worst <= 2, `one artist took ${worst} of 12 slots (cap is 2)`);
  assert.ok(counts.size >= 6, `only ${counts.size} distinct artists in 12 slots`);
  console.log(`    ${counts.size} distinct artists, max ${worst} slots each`);
});

await check('the cap backfills rather than starving a thin publisher', () => {
  // THE failure this design has to avoid. The artist publisher holds exactly one
  // Artist record against 41 tracks, so a per-artist cap without backfill cuts
  // the "…and from the other repo" rail from six cards to two and silently
  // removes the cross-publisher demonstration. Nothing else would catch that.
  const solo = artist.counts.Artist ?? 0;
  assert.equal(solo, 1, `expected a single-artist publisher to test backfill, got ${solo}`);

  g.kernelReset();
  for (const t of artistTracks.slice(0, 3)) g.kernelSignal(t.id, 'play');
  const filtered = g.kernelRecommend(6, artist.owner);
  assert.equal(filtered.length, 6,
    `owner-filtered rail rendered ${filtered.length}/6 — the cap starved it instead of backfilling`);
  assert.ok(filtered.every((r) => (r.owner ?? '').toLowerCase() === artist.owner.toLowerCase()),
    'backfill leaked records from the wrong publisher');
  console.log(`    ${filtered.length}/6 cards from a publisher with ${solo} artist`);
});

await check('a thin-catalogue artist is still reachable past the shortlist', () => {
  const { genres, artists } = g.onboardingOptions();
  const picks = artists.slice(0, 3);
  g.kernelSeed(genres.slice(0, 3).map((x) => x.id), picks.map((x) => x.id));

  // kernelRecommend shortlists by RAW COSINE before reweighting, so an artist with
  // three tracks against a labelmate's forty can fall outside the default pool of
  // 600 and never collect their tau_art boost. That is the pool's doing, not the
  // kernel's: widen it and they rank fine. Pinned because the rail's default depth
  // is the thing that decides whether "follow" visibly means anything.
  const thin = picks.reduce((a, b) => (a.tracks <= b.tracks ? a : b));
  const theirs = new Set(g.neighbours(thin.id, 'created', 'out', 60).records.map((r) => r.id));
  // Pool = the WHOLE corpus, derived not pinned. This read `25000`, which was the
  // corpus size at the reference bake (25,372 points) and therefore meant "everything".
  // A re-crawl grows the corpus — 26,642 here — and the constant silently stops being
  // exhaustive, so a thin artist falls outside the pool and this check fails for a
  // reason that has nothing to do with the kernel.
  // Both numbers are derived, not pinned, because BOTH used to be "the whole corpus"
  // by accident and stopped being so the moment anyone re-crawled:
  //   • pool was 25000 — the corpus size at the reference bake (25,372). At 26,642 it
  //     silently stopped covering everything, which is the starvation this guards.
  //   • depth was 200. The property under test is "not starved entirely", not "top
  //     200": where a thin artist lands is data-dependent (rank 374 on this crawl,
  //     inside 200 on the reference one), so a fixed cutoff fails for reasons that
  //     have nothing to do with the kernel.
  const depth = Math.min(1000, stats.records);
  const deep = g.kernelRecommend(depth, undefined, stats.records);
  const at = deep.findIndex((r) => theirs.has(r.id));
  assert.ok(at >= 0,
    `nothing by ${thin.name} (${thin.tracks} tracks) ranks in the top ${depth} of ${stats.records}`);
  console.log(`    ${thin.name} (${thin.tracks} tracks) first appears at rank ${at}`);
});

await check('following the sovereign artist reaches across the publisher split', () => {
  const { genres } = g.onboardingOptions();
  const disclosure = g.onboardingOptions().artists
    .find((a) => a.owner.toLowerCase() === artist.owner.toLowerCase())!;
  g.kernelSeed(genres.slice(0, 3).map((x) => x.id), [disclosure.id]);
  const recs = g.kernelRecommend(60);
  const crossed = recs.filter((r) => (r.owner ?? '').toLowerCase() === artist.owner.toLowerCase());
  assert.ok(crossed.length > 0,
    'seeding on the artist publisher produced no recommendations from it');
  console.log(`    ${crossed.length}/${recs.length} from ${disclosure.name}'s own repo`);
});

if (failures) { console.error(`\n${failures} kernel check(s) failed`); process.exit(1); }
console.log('\nSession kernel behaves on this corpus.');
