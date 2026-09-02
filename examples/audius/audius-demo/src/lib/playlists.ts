// Your playlists — built here, saved here, never uploaded.
//
// NOT to be confused with the graph's own `Playlist` records (types.ts EntityType,
// `contains -> Track` edges). Those are Audius' published playlists, owned by a
// publisher wallet and the same for everyone. These are yours and local to this
// browser. The UI says "Your playlists" everywhere so the two never read as one
// thing on screen.
//
// ZERO imports and ZERO globals, on purpose: scripts/check-playlists.ts loads this
// module under `node --experimental-strip-types`, which cannot parse JSX and has no
// localStorage. The getItem/setItem wrappers live in playlists.tsx next to the state
// that calls them — the same split kernel.tsx uses.
//
// Every operation below is TOTAL and IMMUTABLE, and returns the input array BY
// REFERENCE when the edit changes nothing. That is load-bearing twice over: React
// skips the re-render for free, and the provider's `commit` uses `next === prev` as
// its "did anything happen" test instead of a deep compare.

export interface Playlist {
  id: string;
  name: string;
  createdAt: number;
  /** Rec.id — the content-address graph id, NOT fields.id. Deduped; order is play order. */
  trackIds: string[];
}

export const PLAYLISTS_KEY = 'audius-demo.playlists';

/** Deliberately its own key, NOT part of `audius-demo.kernel`. kernel.tsx's load()
 *  returns null whenever the embedding dim changes — right for a taste vector, and
 *  silent deletion for a library, since a re-bake would take the playlists with it. */

const UNTITLED = 'Untitled playlist';

const clean = (s: unknown) => (typeof s === 'string' ? s.trim() : '');

/** Non-empty strings, deduped, order preserved. Null if it isn't a list at all. */
function cleanIds(v: unknown): string[] | null {
  if (!Array.isArray(v)) return null;
  const out: string[] = [];
  const seen = new Set<string>();
  for (const x of v) {
    if (typeof x !== 'string' || !x || seen.has(x)) continue;
    seen.add(x);
    out.push(x);
  }
  return out;
}

function cleanOne(v: unknown): Playlist | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  const id = clean(o.id);
  if (!id) return null;
  const trackIds = cleanIds(o.trackIds);
  if (!trackIds) return null;
  const createdAt = Number(o.createdAt);
  return {
    id,
    // A nameless playlist keeps its tracks — they're the part that can't be retyped.
    name: clean(o.name) || UNTITLED,
    createdAt: Number.isFinite(createdAt) ? createdAt : 0,
    trackIds,
  };
}

/**
 * The single trust boundary — for localStorage AND for an imported file. Both are
 * user-writable, so both are untrusted input; one validator, tested once. Never throws.
 *
 * It salvages PER ENTRY rather than giving up on the first bad byte, which is exactly
 * where this differs from kernel.tsx's load(). A kernel is derived state that
 * regenerates in five clicks, so discarding it wholesale is free. A playlist is
 * authored and cannot be recovered — throwing away a library because one entry has a
 * numeric name is data loss, not caution.
 */
export function parse(raw: string | null): Playlist[] {
  if (!raw) return [];
  let data: unknown;
  try { data = JSON.parse(raw); } catch { return []; }
  if (!Array.isArray(data)) return [];
  const out: Playlist[] = [];
  const seen = new Set<string>();
  for (const entry of data) {
    const pl = cleanOne(entry);
    if (!pl || seen.has(pl.id)) continue;
    seen.add(pl.id);
    out.push(pl);
  }
  return out;
}

export function serialize(pls: Playlist[]): string {
  return JSON.stringify(pls);
}

/** Swap one playlist by id. `fn` returning null means "nothing changed". */
function patch(
  pls: Playlist[], id: string, fn: (pl: Playlist) => Playlist | null,
): Playlist[] {
  const i = pls.findIndex((p) => p.id === id);
  if (i < 0) return pls;
  const next = fn(pls[i]);
  if (!next) return pls;
  const copy = pls.slice();
  copy[i] = next;
  return copy;
}

/** The id is passed in rather than generated here — it keeps `crypto` out of this
 *  module (see the header), the provider needs the id immediately to add the track
 *  that prompted the creation, and it makes the check deterministic. */
export function create(
  pls: Playlist[], name: string, id: string, createdAt = Date.now(),
): Playlist[] {
  return [...pls, { id, name: clean(name) || UNTITLED, createdAt, trackIds: [] }];
}

export function remove(pls: Playlist[], id: string): Playlist[] {
  const next = pls.filter((p) => p.id !== id);
  return next.length === pls.length ? pls : next;
}

export function rename(pls: Playlist[], id: string, name: string): Playlist[] {
  const n = clean(name) || UNTITLED;
  return patch(pls, id, (pl) => (pl.name === n ? null : { ...pl, name: n }));
}

export function addTrack(pls: Playlist[], id: string, trackId: string): Playlist[] {
  if (!trackId) return pls;
  return patch(pls, id, (pl) => (pl.trackIds.includes(trackId)
    ? null
    : { ...pl, trackIds: [...pl.trackIds, trackId] }));
}

export function removeTrack(pls: Playlist[], id: string, trackId: string): Playlist[] {
  return patch(pls, id, (pl) => {
    const t = pl.trackIds.filter((x) => x !== trackId);
    return t.length === pl.trackIds.length ? null : { ...pl, trackIds: t };
  });
}

export function reorder(pls: Playlist[], id: string, from: number, to: number): Playlist[] {
  return patch(pls, id, (pl) => {
    const n = pl.trackIds.length;
    if (from === to || from < 0 || to < 0 || from >= n || to >= n) return null;
    const t = pl.trackIds.slice();
    t.splice(to, 0, ...t.splice(from, 1));
    return { ...pl, trackIds: t };
  });
}

/**
 * Import. NEVER destructive: nothing is removed and nothing is renamed.
 *
 * Matching ids union their tracks (yours first, unseen appended); unknown ids are
 * appended whole. So importing the same file twice is a no-op the second time, and
 * importing someone else's export can only ever add.
 */
export function merge(mine: Playlist[], incoming: Playlist[]): Playlist[] {
  if (!incoming.length) return mine;
  let changed = false;
  const grown = mine.map((pl) => {
    const inc = incoming.find((p) => p.id === pl.id);
    if (!inc) return pl;
    const add = inc.trackIds.filter((t) => !pl.trackIds.includes(t));
    if (!add.length) return pl;
    changed = true;
    return { ...pl, trackIds: [...pl.trackIds, ...add] };
  });
  const mineIds = new Set(mine.map((p) => p.id));
  const fresh = incoming.filter((p) => !mineIds.has(p.id));
  if (!changed && !fresh.length) return mine;
  return [...grown, ...fresh];
}

/** Total tracks held, for the "imported N tracks" line. */
export const trackCount = (pls: Playlist[]) =>
  pls.reduce((n, p) => n + p.trackIds.length, 0);
