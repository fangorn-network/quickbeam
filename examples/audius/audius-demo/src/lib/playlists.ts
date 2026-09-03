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

// ── sharing ─────────────────────────────────────────────────────────────────
//
// A share link carries the playlist IN the URL — there is no server to put it on,
// which is the same reason search is a local vector match rather than a request.
// It rides in the hash fragment (see router.ts), the part browsers never transmit,
// so copying a link sends nothing anywhere. The link IS the data.
//
// Base64url and `location` belong to the browser, so they live in playlists.tsx.
// What is here is the shape, and the shape is where the bugs would be.

/** Every id in the snapshot is `audius:<type>:<suffix>` — measured across all 26,642
 *  records, suffix charset `[A-Za-z0-9-]`, never a colon. So this prefix is 13 of the
 *  ~20 characters of a track id, it is the same on every one of them, and dropping it
 *  is reversible by asking whether a colon survived. */
const TRACK_PREFIX = 'audius:track:';

const short = (id: string) =>
  (id.startsWith(TRACK_PREFIX) ? id.slice(TRACK_PREFIX.length) : id);

/** Anything non-string becomes '', which cleanIds drops — see parse. */
const long = (s: unknown) =>
  (typeof s !== 'string' ? '' : s.includes(':') ? s : TRACK_PREFIX + s);

/**
 * The wire form: `[[id, name, ...trackSuffixes], …]`.
 *
 * The playlist's own id rides along. That is what makes re-sharing an EDITED playlist
 * union into the recipient's existing copy instead of landing beside it as a second
 * one — merge() matches by id, so the id is the thing that makes a link idempotent.
 *
 * `createdAt` deliberately does not: it is stamped on arrival, so a playlist someone
 * just saved sorts to the top of the newest-first index rather than to wherever the
 * sender happened to make it.
 *
 * ponytail: no compression. 30 tracks is ~450 chars of URL and a whole library ~2,500,
 * both of which paste fine. CompressionStream would cut that to ~600 and the app
 * already uses its decompress twin (graph.ts), but it is a WEB global and this module
 * is deliberately free of those (see the header) — reach for it only if library links
 * start getting truncated in the wild.
 */
export function toShare(pls: Playlist[]): string {
  return JSON.stringify(pls.map((p) => [p.id, p.name, ...p.trackIds.map(short)]));
}

/**
 * Read the wire form back. NEVER throws, and deliberately does not grow a validator
 * of its own: it rebuilds whole Playlists and hands them to parse(), which is already
 * THE trust boundary for untrusted input and already salvages per entry. A URL is
 * untrusted in exactly the way localStorage and an imported file are — one validator,
 * tested once.
 *
 * `now` is a parameter rather than a call for the same reason create()'s is: it keeps
 * the check deterministic.
 */
export function fromShare(wire: string, now = Date.now()): Playlist[] {
  let data: unknown;
  try { data = JSON.parse(wire); } catch { return []; }
  if (!Array.isArray(data)) return [];
  return parse(JSON.stringify(data.map((e) => (Array.isArray(e)
    ? { id: e[0], name: e[1], createdAt: now, trackIds: e.slice(2).map(long) }
    : null))));
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
