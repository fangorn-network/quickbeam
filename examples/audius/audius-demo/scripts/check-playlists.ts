// Does the playlist store hold up against input it doesn't control?
//
// Playlists are the only AUTHORED state in this app — everything else (the graph, the
// taste kernel) regenerates from the snapshot or from five clicks. So the failure that
// matters here is not "wrong recommendation", it's "the library is gone", and every
// assertion below is aimed at one of the two ways that happens: a validator that gives
// up on the whole blob over one bad entry, or a mutation that quietly drops entries.
//
// Unlike every other check in this repo it needs NO cdn serve, NO Qdrant and NO
// network — the module under test has zero imports and zero globals, so there is
// nothing to stub either.
//
// Usage: npm run check:playlists
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  PLAYLISTS_KEY, addTrack, create, merge, parse, remove, removeTrack, rename,
  reorder, serialize, trackCount, type Playlist,
} from '../src/lib/playlists.ts';

let failures = 0;
async function check(name: string, fn: () => void | Promise<void>) {
  try { await fn(); console.log(`✓ ${name}`); }
  catch (e) { failures++; console.log(`✗ ${name}\n    ${(e as Error).message}`); }
}

const pl = (id: string, name: string, trackIds: string[] = []): Playlist =>
  ({ id, name, createdAt: 1, trackIds });

const base = [pl('a', 'Night drive', ['t1', 't2', 't3']), pl('b', 'Garage', ['t9'])];
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

// ── the validator ───────────────────────────────────────────────────────────

await check('parse never throws and yields [] on junk', () => {
  for (const raw of [null, '', '   ', '{', 'null', 'undefined', '{"a":1}', '[1,2,3]',
                     '"a string"', '[[]]', 'NaN']) {
    assert.deepEqual(parse(raw), [], `raw=${JSON.stringify(raw)}`);
  }
});

// THE anti-regression assertion. kernel.tsx's load() returns null on any suspect
// input; copying that here would delete a whole library over one malformed entry.
// If someone "simplifies" parse() into the all-or-nothing form, this is what fails.
await check('parse SALVAGES the good entries beside a malformed one', () => {
  const raw = JSON.stringify([
    { id: 'a', name: 'Keep me', createdAt: 5, trackIds: ['t1', 't2'] },
    { garbage: true },
    null,
    'nope',
    { id: '', name: 'no id', trackIds: [] },
    { id: 'c', name: 'no trackIds' },
    { id: 'd', name: 'Also keep', createdAt: 6, trackIds: ['t3'] },
  ]);
  const out = parse(raw);
  assert.deepEqual(out.map((p) => p.id), ['a', 'd']);
  assert.deepEqual(out[0].trackIds, ['t1', 't2']);
});

await check('parse repairs a salvageable entry rather than dropping it', () => {
  const out = parse(JSON.stringify([
    { id: 'a', name: 42, createdAt: 'soon', trackIds: ['t1', '', 't1', 7, 't2'] },
  ]));
  assert.equal(out.length, 1, 'entry was dropped instead of repaired');
  assert.equal(out[0].name, 'Untitled playlist', 'a bad name must not cost the tracks');
  assert.equal(out[0].createdAt, 0);
  assert.deepEqual(out[0].trackIds, ['t1', 't2'], 'trackIds must be deduped and cleaned');
});

await check('parse drops duplicate playlist ids, keeping the first', () => {
  const out = parse(JSON.stringify([pl('a', 'First', ['t1']), pl('a', 'Second', ['t2'])]));
  assert.equal(out.length, 1);
  assert.equal(out[0].name, 'First');
});

await check('serialize -> parse round-trips', () => {
  assert.deepEqual(parse(serialize(base)), base);
});

// ── the operations ──────────────────────────────────────────────────────────

await check('addTrack appends and dedupes', () => {
  assert.deepEqual(addTrack(base, 'a', 't4')[0].trackIds, ['t1', 't2', 't3', 't4']);
  assert.equal(addTrack(base, 'a', 't2'), base, 'a duplicate must be a no-op');
});

await check('removeTrack removes; removing an absent track is a no-op', () => {
  assert.deepEqual(removeTrack(base, 'a', 't2')[0].trackIds, ['t1', 't3']);
  assert.equal(removeTrack(base, 'a', 'nope'), base);
});

await check('rename and remove', () => {
  assert.equal(rename(base, 'a', 'Renamed')[0].name, 'Renamed');
  assert.equal(rename(base, 'a', '   ')[0].name, 'Untitled playlist');
  assert.equal(rename(base, 'a', 'Night drive'), base, 'renaming to itself is a no-op');
  assert.deepEqual(remove(base, 'a').map((p) => p.id), ['b']);
});

await check('create appends an empty playlist with the id it was given', () => {
  const out = create(base, '  Fresh  ', 'zz', 99);
  assert.equal(out.length, 3);
  assert.deepEqual(out[2], { id: 'zz', name: 'Fresh', createdAt: 99, trackIds: [] });
  assert.equal(create(base, '', 'zz', 99)[2].name, 'Untitled playlist');
});

await check('reorder moves a track to the target index', () => {
  assert.deepEqual(reorder(base, 'a', 0, 2)[0].trackIds, ['t2', 't3', 't1']);
  assert.deepEqual(reorder(base, 'a', 2, 0)[0].trackIds, ['t3', 't1', 't2']);
  assert.deepEqual(reorder(base, 'a', 0, 1)[0].trackIds, ['t2', 't1', 't3']);
});

// The arrow buttons at the ends and a drag onto its own row both land here.
await check('reorder is a no-op when it cannot move', () => {
  for (const [from, to] of [[1, 1], [-1, 0], [0, -1], [0, 3], [3, 0], [9, 9]]) {
    assert.equal(reorder(base, 'a', from, to), base, `from=${from} to=${to}`);
  }
});

await check('every op returns the SAME reference for an unknown playlist id', () => {
  assert.equal(addTrack(base, 'nope', 't1'), base);
  assert.equal(removeTrack(base, 'nope', 't1'), base);
  assert.equal(rename(base, 'nope', 'x'), base);
  assert.equal(remove(base, 'nope'), base);
  assert.equal(reorder(base, 'nope', 0, 1), base);
});

await check('no op mutates its input', () => {
  const before = clone(base);
  create(base, 'x', 'zz'); remove(base, 'a'); rename(base, 'a', 'x');
  addTrack(base, 'a', 't7'); removeTrack(base, 'a', 't1'); reorder(base, 'a', 0, 2);
  merge(base, [pl('a', 'Other', ['t8'])]);
  assert.deepEqual(base, before);
});

// ── import ──────────────────────────────────────────────────────────────────

await check('merge unions a matching id, keeping my order and my name', () => {
  const out = merge(base, [pl('a', 'Their name', ['t3', 't7'])]);
  assert.equal(out.length, 2);
  assert.equal(out[0].name, 'Night drive', 'an import must never rename my playlist');
  assert.deepEqual(out[0].trackIds, ['t1', 't2', 't3', 't7']);
});

await check('merge appends an unknown playlist and removes nothing', () => {
  const out = merge(base, [pl('z', 'Theirs', ['t5'])]);
  assert.deepEqual(out.map((p) => p.id), ['a', 'b', 'z']);
  assert.equal(trackCount(out), trackCount(base) + 1);
});

// Importing the same file twice is the likeliest real mistake.
await check('merge is idempotent', () => {
  const incoming = [pl('a', 'Theirs', ['t3', 't7']), pl('z', 'New', ['t5'])];
  const once = merge(base, incoming);
  assert.deepEqual(merge(once, incoming), once);
  assert.equal(merge(once, incoming), once, 'a second identical import must be a no-op');
  assert.equal(merge(base, []), base);
});

await check('importing a hostile file never throws and never drops what I have', () => {
  for (const raw of ['[]', 'null', '[{"id":"a","trackIds":"not a list"}]', 'garbage',
                     '[{"id":"a","name":null,"trackIds":[null,{}]}]']) {
    const out = merge(base, parse(raw));
    assert.ok(out.length >= base.length, `raw=${raw}`);
    for (const mine of base) {
      const kept = out.find((p) => p.id === mine.id);
      assert.ok(kept, `lost playlist ${mine.id} importing ${raw}`);
      for (const t of mine.trackIds) assert.ok(kept.trackIds.includes(t), `lost ${t}`);
    }
  }
});

// ── the two claims outside this file ────────────────────────────────────────

await check('the storage key is its own, and is not the kernel\'s', () => {
  assert.equal(PLAYLISTS_KEY, 'audius-demo.playlists');
  assert.notEqual(PLAYLISTS_KEY, 'audius-demo.kernel');
});

// Privacy.tsx enumerates what this app puts in localStorage and its header comment
// says "if any of that changes, change this page with it". check-private.ts already
// reads source files in a check; this turns that sentence from a hope into a failure.
await check('Privacy.tsx names the playlist storage key', () => {
  const src = readFileSync(new URL('../src/views/Privacy.tsx', import.meta.url), 'utf8');
  assert.ok(src.includes(PLAYLISTS_KEY),
    `Privacy.tsx must name ${PLAYLISTS_KEY} — it tells the reader what is stored`);
});

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log('\nall playlist checks passed');
