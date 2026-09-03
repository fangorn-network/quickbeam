// Do the agent tools hold up without a browser?
//
// The failures that matter here are different from the other checks. These tools are
// called by something that cannot see the screen and will pass whatever it inferred
// from a schema, so the two ways this breaks are: a tool THROWS instead of answering
// (an agent gets an opaque transport error rather than a sentence it can act on), and
// a write happens that the person did not agree to.
//
// The third is subtler and is why the projection is asserted field by field: drop
// `duration` from what search returns and every tool still "works", while the one
// request this feature exists for — "make me an hour of music that starts high and
// tapers off" — becomes unanswerable. That is a silent capability regression, so it
// gets an explicit test.
//
// Needs NO cdn serve, NO Qdrant, NO network and no browser: webmcp.ts has zero imports
// (client.ts builds a Worker at module scope, which would throw here), so the fake `mc`
// and deps below are the entire harness.
//
// Usage: npm run check:webmcp
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  TOOL_NAMES, brief, clock, registerTools,
  type Deps, type PlaylistLike, type RecLike, type Tool,
} from '../src/lib/webmcp.ts';

let failures = 0;
async function check(name: string, fn: () => void | Promise<void>) {
  try { await fn(); console.log(`✓ ${name}`); }
  catch (e) { failures++; console.log(`✗ ${name}\n    ${(e as Error).message}`); }
}

// ── the harness ─────────────────────────────────────────────────────────────

const track = (id: string, title: string, mood: string, duration: number): RecLike => ({
  id: `audius:track:${id}`,
  entityType: 'Track',
  owner: '0x1111111111111111111111111111111111111111',
  fields: { id, title, artist: 'MR•CAR/\\ACK', genre: 'Techno', mood, duration },
});

// A small corpus with a real energy gradient, like the bake's.
const CORPUS: RecLike[] = [
  track('hot1', 'Peak Time', 'Energizing', 300),
  track('hot2', 'Fire', 'Fiery', 300),
  track('mid1', 'Groove', 'Cool', 300),
  track('mid2', 'Drift', 'Easygoing', 300),
  track('low1', 'Comedown', 'Peaceful', 300),
  track('low2', 'Last Call', 'Melancholy', 300),
];
const byId = new Map(CORPUS.map((r) => [r.id, r]));

interface Spy {
  searched: Array<{ q: string }>;
  navigated: string[];
  asked: string[];
  answer: boolean;
  lists: PlaylistLike[];
  played: string[];
}

function harness(over: Partial<Deps> = {}) {
  const spy: Spy = { searched: [], navigated: [], asked: [], answer: true, lists: [], played: [] };
  const deps: Deps = {
    search: async (q) => { spy.searched.push({ q }); return CORPUS; },
    entity: async (id) => byId.get(id) ?? null,
    entities: async (ids) => ids.map((i) => byId.get(i) ?? null),
    relations: async () => [{ rel: 'sameAs', dir: 'out', count: 1, crosses: true }],
    neighbours: async () => ({ records: [CORPUS[0]], total: 1 }),
    sample: async () => CORPUS,
    stats: async () => ({ records: 6, publishers: 2 }),
    taste: async () => ({ topMoods: [['Energizing', 1]] }),
    recommend: async () => CORPUS.slice(0, 2),
    player: () => ({
      current: null, playing: false, time: 0, duration: 0,
      play: (r) => { spy.played.push(r.id); }, toggle: () => {}, seek: () => {},
      next: () => {}, prev: () => {},
    }),
    playlists: () => spy.lists,
    createPlaylist: (name, trackIds) => {
      const pl = { id: `pl-${spy.lists.length}`, name, trackIds };
      spy.lists = [...spy.lists, pl];
      return pl;
    },
    addToPlaylist: (id, trackIds) => {
      const pl = spy.lists.find((p) => p.id === id)!;
      const add = trackIds.filter((t) => !pl.trackIds.includes(t));
      spy.lists = spy.lists.map((p) =>
        (p.id === id ? { ...p, trackIds: [...p.trackIds, ...add] } : p));
      return { added: add.length };
    },
    shareUrl: (pls) => `https://x/#/playlists?s=${pls.length}`,
    confirm: async (q) => { spy.asked.push(q); return spy.answer; },
    goSearch: (q) => { spy.navigated.push(`search:${q}`); },
    goEntity: (id) => { spy.navigated.push(`entity:${id}`); },
    goPlaylists: (id) => { spy.navigated.push(`playlists:${id ?? ''}`); },
    ...over,
  };

  const tools = new Map<string, Tool>();
  // Honours the signal the way the browser does — otherwise the abort assertion below
  // passes for the wrong reason.
  const mc = {
    registerTool: (t: Tool, o?: { signal?: AbortSignal }) => {
      tools.set(t.name, t);
      o?.signal?.addEventListener('abort', () => tools.delete(t.name));
    },
  };
  const off = registerTools(mc, () => deps);
  /** Call a tool and parse the single text block back out. */
  const run = async (name: string, args: Record<string, unknown> = {}) => {
    const r = await tools.get(name)!.execute(args);
    const body = r.content[0].text;
    try { return JSON.parse(body); } catch { return body; }
  };
  return { tools, run, off, spy };
}

// ── the registry ────────────────────────────────────────────────────────────

await check('every declared tool registers, in order', () => {
  const { tools } = harness();
  assert.deepEqual([...tools.keys()], [...TOOL_NAMES]);
  assert.equal(tools.size, 14);
});

await check('every tool has a description and a well-formed object schema', () => {
  const { tools } = harness();
  for (const t of tools.values()) {
    assert.ok(t.description.length > 40, `${t.name} needs a description an agent can act on`);
    assert.equal(t.inputSchema.type, 'object', t.name);
    assert.equal(typeof t.inputSchema.properties, 'object', t.name);
    for (const r of t.inputSchema.required ?? []) {
      assert.ok(r in t.inputSchema.properties, `${t.name}: required "${r}" is not a property`);
    }
  }
});

await check('abort unregisters every tool', () => {
  const { tools, off } = harness();
  off();
  assert.equal(tools.size, 0);
});

// ── the projection: what makes composing possible ───────────────────────────

// THE anti-regression assertion for this feature. If someone trims `brief` to "keep
// payloads small", the party-playlist request silently stops being answerable — the
// agent can still search, it just cannot add up an hour or read the energy.
await check('brief carries duration and mood — an agent cannot compose without them', () => {
  const b = brief(track('x', 'T', 'Energizing', 188));
  assert.equal(b.duration, 188, 'duration is how a length brief gets answered');
  assert.equal(b.mood, 'Energizing', 'mood is how an energy arc gets shaped');
  assert.equal(b.id, 'audius:track:x');
  assert.equal(b.title, 'T');
});

await check('brief drops a zero duration rather than reporting 0 seconds', () => {
  assert.equal(brief(track('x', 'T', 'Cool', 0)).duration, undefined);
});

await check('search results keep the projection', async () => {
  const { run } = harness();
  const out = await run('search-music', { query: 'techno' });
  assert.ok(Array.isArray(out));
  assert.equal(out[0].duration, 300);
  assert.ok(out[0].mood);
});

await check('clock formats minutes and hours', () => {
  assert.equal(clock(0), '0:00');
  assert.equal(clock(3504), '58:24');
  assert.equal(clock(3720), '1:02:00');
});

// ── errors are answers, never throws ────────────────────────────────────────

// An agent that gets a throw sees an opaque transport failure it cannot act on; a
// sentence tells it what to do next.
await check('an unknown id is a message, not a throw', async () => {
  const { run } = harness();
  assert.match(await run('open-record', { id: 'audius:track:nope' }), /No record/);
  assert.match(await run('add-to-playlist', { playlist: 'ghost', trackIds: ['x'] }), /No playlist/);
  assert.match(await run('share-playlist', { playlist: 'ghost' }), /No playlist/);
});

await check('a failing dependency is reported, not thrown', async () => {
  const { run } = harness({ search: async () => { throw new Error('worker died'); } });
  assert.match(await run('search-music', { query: 'x' }), /Search failed: worker died/);
});

await check('missing and empty arguments are answered', async () => {
  const { run } = harness();
  assert.match(await run('search-music', { query: '   ' }), /something to search for/);
  assert.match(await run('create-playlist', { name: '', trackIds: ['a'] }), /needs a name/);
  assert.match(await run('create-playlist', { name: 'x', trackIds: [] }), /at least one track/);
  assert.match(await run('control-player', { action: 'wat' }), /Unknown action/);
  assert.match(await run('control-player', { action: 'seek' }), /needs `seconds`/);
});

// ── moving the person's page ────────────────────────────────────────────────

await check('search paints the page by default and stays put when asked not to', async () => {
  const { run, spy } = harness();
  await run('search-music', { query: 'techno' });
  assert.deepEqual(spy.navigated, ['search:techno']);
  // The composing case: several searches in a row must not make the page jump.
  await run('search-music', { query: 'mellow', show: false });
  await run('search-music', { query: 'ambient', show: false });
  assert.deepEqual(spy.navigated, ['search:techno'], 'show:false must not navigate');
});

await check('list-relations surfaces the cross-publisher flag', async () => {
  const { run } = harness();
  const out = await run('list-relations', { id: CORPUS[0].id });
  assert.equal(out[0].crosses, true);
});

await check('control-player refuses a non-track', async () => {
  const artist: RecLike = { id: 'audius:user:a', entityType: 'Artist', fields: { handle: 'x' } };
  const { run } = harness({ entity: async () => artist });
  assert.match(await run('control-player', { action: 'play', id: 'audius:user:a' }), /not a track/);
});

// ── the writes, and consent ─────────────────────────────────────────────────

await check('create-playlist saves WITHOUT asking, and reports the running time', async () => {
  const { run, spy } = harness();
  const out = await run('create-playlist', {
    name: 'party tonight',
    trackIds: CORPUS.map((r) => r.id),
  });
  assert.deepEqual(spy.asked, [], 'a person asking for a playlist must not be interrupted');
  assert.equal(spy.lists.length, 1);
  assert.equal(spy.lists[0].name, 'party tonight');
  assert.equal(out.duration, 1800);
  assert.equal(out.runningTime, '30:00');
  assert.ok(out.link, 'the agent needs a link to hand back');
});

await check('create-playlist keeps the order it was given — the arc is the order', async () => {
  const { spy, run } = harness();
  const ordered = ['hot1', 'hot2', 'mid1', 'low1'].map((x) => `audius:track:${x}`);
  await run('create-playlist', { name: 'arc', trackIds: ordered });
  assert.deepEqual(spy.lists[0].trackIds, ordered);
});

await check('create-playlist skips ids not in the snapshot and says which', async () => {
  const { run, spy } = harness();
  const out = await run('create-playlist', {
    name: 'mixed', trackIds: [CORPUS[0].id, 'audius:track:ghost'],
  });
  assert.deepEqual(out.skipped, ['audius:track:ghost']);
  assert.deepEqual(spy.lists[0].trackIds, [CORPUS[0].id]);
});

await check('create-playlist saves nothing when no id resolves', async () => {
  const { run, spy } = harness();
  assert.match(await run('create-playlist', { name: 'x', trackIds: ['audius:track:ghost'] }),
    /Nothing was saved/);
  assert.equal(spy.lists.length, 0);
});

await check('add-to-playlist ASKS, and adds when the person agrees', async () => {
  const { run, spy } = harness();
  await run('create-playlist', { name: 'mine', trackIds: [CORPUS[0].id] });
  spy.asked = [];
  const out = await run('add-to-playlist', { playlist: 'mine', trackIds: [CORPUS[1].id] });
  assert.equal(spy.asked.length, 1, 'changing something of theirs must ask');
  assert.match(spy.asked[0], /Add 1 track to "mine"\?/);
  assert.equal(out.added, 1);
  assert.equal(spy.lists[0].trackIds.length, 2);
});

// The whole point of the card. A declined ask must leave the library byte-identical.
await check('a declined ask changes NOTHING', async () => {
  const { run, spy } = harness();
  await run('create-playlist', { name: 'mine', trackIds: [CORPUS[0].id] });
  const before = JSON.stringify(spy.lists);
  spy.answer = false;
  const msg = await run('add-to-playlist', { playlist: 'mine', trackIds: [CORPUS[1].id] });
  assert.match(msg, /did not accept/);
  assert.equal(JSON.stringify(spy.lists), before);
});

await check('add-to-playlist resolves a playlist by name, case-insensitively', async () => {
  const { run, spy } = harness();
  await run('create-playlist', { name: 'Night Drive', trackIds: [CORPUS[0].id] });
  await run('add-to-playlist', { playlist: 'night drive', trackIds: [CORPUS[1].id] });
  assert.equal(spy.lists[0].trackIds.length, 2);
});

await check('add-to-playlist does not ask when there is nothing new to add', async () => {
  const { run, spy } = harness();
  await run('create-playlist', { name: 'mine', trackIds: [CORPUS[0].id] });
  spy.asked = [];
  assert.match(await run('add-to-playlist', { playlist: 'mine', trackIds: [CORPUS[0].id] }),
    /Nothing new/);
  assert.deepEqual(spy.asked, [], 'a no-op must not put a card on someone');
});

await check('no tool can delete or rename', () => {
  const { tools } = harness();
  for (const n of tools.keys()) {
    assert.doesNotMatch(n, /delete|remove|rename|clear/, `${n} must not exist`);
  }
});

// ── the composing scenario, end to end ──────────────────────────────────────

// The request this feature exists to answer, driven through the tools exactly as an
// agent would. If this fails, the tool surface is not sufficient for the task.
await check('an agent can build "an hour, high energy tapering off"', async () => {
  const hour: RecLike[] = [];
  for (let i = 0; i < 20; i++) {
    const mood = i < 7 ? 'Energizing' : i < 14 ? 'Cool' : 'Peaceful';
    hour.push(track(`t${i}`, `Track ${i}`, mood, 180));
  }
  const map = new Map(hour.map((r) => [r.id, r]));
  const { run, spy } = harness({
    search: async (q) => hour.filter((r) =>
      (/high|energy|peak/.test(q) && r.fields.mood === 'Energizing')
      || (/mid|groove/.test(q) && r.fields.mood === 'Cool')
      || (/mellow|down|calm/.test(q) && r.fields.mood === 'Peaceful')),
    entities: async (ids) => ids.map((i) => map.get(i) ?? null),
  });

  // 1. gather down the gradient, without moving the person's page
  const peak = await run('search-music', { query: 'peak time high energy', show: false });
  const mid = await run('search-music', { query: 'mid tempo groove', show: false });
  const calm = await run('search-music', { query: 'mellow calm comedown', show: false });
  assert.deepEqual(spy.navigated, [], 'gathering must not move the page');

  // 2. the agent budgets the hour itself, from the durations the projection carried
  const arc = [...peak, ...mid, ...calm];
  const picked: string[] = [];
  let secs = 0;
  for (const r of arc) {
    if (secs + r.duration > 3600) break;
    picked.push(r.id); secs += r.duration;
  }
  assert.equal(picked.length, 20);
  assert.equal(secs, 3600);

  // 3. one call, no card
  const out = await run('create-playlist', { name: 'party tonight', trackIds: picked });
  assert.deepEqual(spy.asked, []);
  assert.equal(out.runningTime, '1:00:00');
  assert.equal(out.tracks, 20);

  // 4. it came out shaped: hot at the front, calm at the back
  const saved = spy.lists[0].trackIds.map((id) => map.get(id)!.fields.mood);
  assert.equal(saved[0], 'Energizing');
  assert.equal(saved[saved.length - 1], 'Peaceful');
  assert.ok(!saved.slice(0, 7).includes('Peaceful'), 'the wind-down must not be at the front');
});

// ── the claims outside this file ────────────────────────────────────────────

await check('App.tsx mounts the tools', () => {
  const src = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(src, /<AgentTools \/>/);
});

// The path almost every visitor takes. It cannot be exercised from a flagged browser
// (deleting document.modelContext at runtime is too late — the effect has already run,
// and a reload restores it), so it is asserted at the source instead: the mount must
// be a plain early return on a feature check, with nothing before it that could throw.
await check('the mount bails silently when the browser has no agent', () => {
  const src = readFileSync(new URL('../src/lib/webmcp.tsx', import.meta.url), 'utf8');
  assert.match(src, /if \(!document\.modelContext\?\.registerTool\) return;/,
    'the no-agent path must be an optional-chained early return');
  // Nothing may touch document.modelContext outside that guard and the line after it.
  const uses = [...src.matchAll(/document\.modelContext/g)].length;
  assert.equal(uses, 2, 'document.modelContext should be read only to check it, then use it');
});

// Same house pattern as check-playlists.ts: the policy makes a falsifiable claim about
// what an agent in this browser can reach, and shipping the tools without it would
// make the page lie.
await check('Privacy.tsx says an agent can read the taste model', () => {
  const src = readFileSync(new URL('../src/views/Privacy.tsx', import.meta.url), 'utf8');
  assert.match(src, /read-taste/, 'Privacy.tsx must name the tool that reads the kernel');
  assert.match(src, /agent/i);
});

await check('About.tsx lists the tools it claims to expose', () => {
  const src = readFileSync(new URL('../src/views/About.tsx', import.meta.url), 'utf8');
  for (const n of ['search-music', 'create-playlist', 'read-taste']) {
    assert.ok(src.includes(n), `About.tsx should name ${n}`);
  }
});

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log(`\nall ${TOOL_NAMES.length} tools check out`);
