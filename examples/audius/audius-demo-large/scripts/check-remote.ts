// The thing Stage 4 exists for: a record that is NOT in the bootstrap must be
// searchable, openable, have real relation rails, and be playable — and getting all
// of that must still disclose nothing about the query beyond a bucket id.
//
// Before this wiring, clicking a search result rendered "Not in this snapshot"
// (Entity.tsx) because entity()/relations()/neighbours() were lookups over the
// resident corpus only. That failure is invisible to every other check, because every
// other check uses records the bootstrap happens to hold.
//
// Usage: cdn serve on :8092, quickbeam serve on :8081 (with --index-layout and
//        --adjacency-db), then:
//   VITE_CDN_URL=http://localhost:8092 VITE_API_URL=http://localhost:8081 \
//   VITE_DOMAIN=audius-home VITE_INDEX_DOMAIN=audius-large npm run check:remote
import assert from 'node:assert/strict';
import { Graph } from '../src/lib/graph.ts';
import type { Rec } from '../src/lib/types.ts';

// Inlined rather than imported: player.tsx pulls in React, which a node check must
// not. Mirrors src/lib/player.tsx `isPlayable` — keep them in step.
const isPlayable = (r: Rec) => r.entityType === 'Track' && !!r.fields.id;

const CDN = process.env.VITE_CDN_URL ?? 'http://localhost:8092';
const DOMAIN = process.env.VITE_DOMAIN ?? 'audius-home';
const PLATFORM = process.env.VITE_PLATFORM_OWNER
  ?? '0x1111111111111111111111111111111111111111';

let failures = 0;
async function check(name: string, fn: () => void | Promise<void>) {
  try { await fn(); console.log(`✓ ${name}`); }
  catch (e) { failures++; console.log(`✗ ${name}\n    ${(e as Error).message}`); }
}

const g = new Graph();
const stats = await g.load(CDN, DOMAIN, PLATFORM);
console.log(`· bootstrap: ${g.size} records`);

// A query deliberately unlikely to be answered well by 41k popular tracks alone.
const hits = await g.search('gregorian chant choir cathedral reverb', 40);
const resident = new Set<string>();
for (let i = 0; i < g.size; i++) resident.add((g as unknown as { recs: Array<{ id: string }> }).recs[i].id);
const remote = hits.filter((h) => !resident.has(h.id));

await check('search reaches beyond the bootstrap', () => {
  assert.ok(hits.length > 0, 'no results at all');
  assert.ok(remote.length > 0,
    `every hit was already resident (${hits.length} hits) — the bucket path is not being used`);
  console.log(`    ${remote.length}/${hits.length} hits came from the hosted corpus`);
});

if (remote.length) {
  const subject = remote[0];

  await check('a non-resident hit opens instead of "Not in this snapshot"', async () => {
    const rec = await g.entityAsync(subject.id);
    assert.ok(rec, `entityAsync returned null for ${subject.id}`);
    assert.equal(rec!.id, subject.id);
    console.log(`    ${rec!.fields.title ?? rec!.id}`);
  });

  await check('its entity page has real relation rails', async () => {
    const groups = await g.relationsAsync(subject.id);
    assert.ok(groups.length > 0, 'no relation groups — the page would read as an orphan');
    const top = groups.slice(0, 3).map((x) => `${x.dir}:${x.rel}=${x.count}`).join(' ');
    console.log(`    ${groups.length} groups — ${top}`);
  });

  await check('a rail resolves to nameable records', async () => {
    const groups = await g.relationsAsync(subject.id);
    const gr = groups[0];
    const { records } = await g.neighboursAsync(subject.id, gr.rel, gr.dir, 8);
    assert.ok(records.length > 0, `${gr.dir}:${gr.rel} resolved to nothing`);
    assert.ok(records.every((r) => r.id), 'a neighbour came back without an id');
  });

  await check('it is playable — fields.id survived the round trip', async () => {
    const rec = await g.entityAsync(subject.id);
    if (rec!.entityType !== 'Track') { console.log('    (not a Track; skipped)'); return; }
    assert.ok(isPlayable(rec!), 'no fields.id — the stream URL cannot be built');
  });

  await check('the kernel LEARNS from a non-resident record', async () => {
    // The sharpest silent failure: kernelSignal no-ops when it cannot find a vector,
    // so playback looks perfectly normal while the recommender learns nothing.
    const before = JSON.stringify(g.kernelSnapshot());
    g.kernelSignal(subject.id, 'play');
    const after = JSON.stringify(g.kernelSnapshot());
    assert.notEqual(before, after,
      'kernel state unchanged — the vector did not survive the bucket fetch');
  });
}

console.log(failures ? `\n${failures} check(s) failed.` : '\nRemote-record path verified.');
process.exit(failures ? 1 : 0);
