// In-memory mock data source — a "fake Qdrant" used when no real collection is
// available (the default for the browser build before real CDN shards exist). It
// implements the same surface as the REST client (scroll / search / count /
// getPoint / recommend) over a generated MusicBrainz-shaped dataset, so the whole
// app — including live role inference (lib/roles.ts) and semantic search — runs
// unchanged.
//
// Document vectors get topical structure from the toy space in lib/mockSpace.ts, so
// `recommend` ("similar entries") returns thematically related items — exactly what
// the real served embeddings will do. Search is plain keyword matching (no query
// embedding). This is the same seam the future ShardDataSource (real CDN vectors +
// in-browser cosine) plugs into: swap the impl, keep the signatures.
import type { QdrantPoint } from './types';
import type { AtlasRaw } from './atlasTypes';
import type { CollectionInfo, Filter, ScrollResult } from './qdrant';
import { QdrantError } from './qdrant';
import { MOCK_DIM, embedTokens, cosine } from './mockSpace';

const DIM = MOCK_DIM;

// ---- deterministic RNG so the dataset is stable across reloads ----
function mulberry32(seed: number): () => number {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rand = mulberry32(42);
const pick = <T,>(a: readonly T[]): T => a[Math.floor(rand() * a.length)];
const sampleN = <T,>(a: readonly T[], n: number): T[] => {
  const c = [...a];
  const out: T[] = [];
  while (out.length < n && c.length) out.push(c.splice(Math.floor(rand() * c.length), 1)[0]);
  return out;
};
const year = (lo: number, hi: number) => String(lo + Math.floor(rand() * (hi - lo)));
const isoDate = () => `${year(1968, 2024)}-${String(1 + Math.floor(rand() * 12)).padStart(2, '0')}-${String(1 + Math.floor(rand() * 28)).padStart(2, '0')}`;

// ---- vocab ----
const AREAS = ['Sheffield', 'London', 'Berlin', 'Chicago', 'Tokyo', 'Reykjavík', 'Lagos', 'Detroit'];
const GENRES = ['indie rock', 'jazz', 'techno', 'folk', 'hip hop', 'ambient', 'soul', 'post-punk', 'classical', 'house'];
const MOODS = ['melancholic', 'energetic', 'dreamy', 'aggressive', 'warm', 'nocturnal', 'uplifting', 'hypnotic'];
const ARTISTS = ['Arctic Monkeys', 'Miles Davis', 'Aphex Twin', 'Nina Simone', 'Burial', 'Fela Kuti', 'Radiohead',
  'J Dilla', 'Björk', 'Kraftwerk', 'Sufjan Stevens', 'Floating Points', 'Sade', 'Four Tet'];
const W1 = ['Midnight', 'Velvet', 'Gold', 'Static', 'Neon', 'Glass', 'Ash', 'Fever', 'Crystal', 'Hollow', 'Paper', 'Iron'];
const W2 = ['Echoes', 'Rivers', 'Bloom', 'Horizon', 'Machine', 'Dreams', 'Light', 'Tide', 'Cities', 'Ghosts', 'Wires', 'Suns'];
const title2 = () => `${pick(W1)} ${pick(W2)}`;

// ---- dataset ----
interface MockPoint extends QdrantPoint {
  vector: number[];
}

// The tokens that define a record's place in the toy vector space — its genres,
// moods, title words, byline and area. Token overlap with a query → high cosine.
function salientTokens(type: string, fields: Record<string, unknown>): string[] {
  const toks: string[] = [type];
  const push = (v: unknown) => {
    if (Array.isArray(v)) v.forEach((x) => typeof x === 'string' && toks.push(x));
    else if (typeof v === 'string') toks.push(...v.split(/[^a-z0-9]+/i));
  };
  push(fields.genres); push(fields.moods); push(fields.title); push(fields.area); push(fields.byArtist);
  return toks;
}

function generate(): MockPoint[] {
  const points: MockPoint[] = [];
  const add = (type: string, fields: Record<string, unknown>) => {
    const i = points.length;
    const mbid = `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;
    const full = { ...fields, entityType: type, schemaVersion: 3, mbid };
    points.push({
      id: `m-${i}`,
      vector: embedTokens(salientTokens(type, full)),
      payload: {
        id: mbid,
        entityType: type,
        owner: '0xMockOwner000000000000000000000000000000',
        meta: { manifestCid: 'QmMockManifestCid' },
        fields: full,
      },
    });
  };

  AREAS.forEach((name) =>
    add('Area', { title: name, areaType: pick(['Country', 'City', 'Subdivision']),
      text: `${name} is a region associated with a distinctive musical scene.` }));

  ARTISTS.forEach((name) => {
    const g = sampleN(GENRES, 2);
    add('Artist', {
      title: name, sortName: name, area: pick(AREAS), artistType: pick(['Group', 'Person']),
      beginYear: year(1960, 2010), gender: pick(['', 'Male', 'Female', 'Non-binary']),
      genres: g, moods: sampleN(MOODS, 2), tags: g.join(', '), rating: +(2 + rand() * 3).toFixed(1),
      text: `${name} is an artist known for ${g.join(' and ')}.`,
    });
  });

  for (let i = 0; i < 10; i++) {
    const g = sampleN(GENRES, 1);
    add('Work', { title: title2(), workType: pick(['Song', 'Symphony', 'Suite']),
      genres: g, tags: g.join(', '), text: 'An underlying composition.' });
  }

  for (let i = 0; i < 30; i++) {
    const g = sampleN(GENRES, 2);
    add('Recording', {
      title: title2(), byArtist: pick(ARTISTS), durationMs: 90000 + Math.floor(rand() * 300000),
      isrcCodes: `US${pick(W1).slice(0, 3).toUpperCase()}${year(10, 24)}${String(Math.floor(rand() * 99999)).padStart(5, '0')}`,
      genres: g, moods: sampleN(MOODS, 2), datePublished: isoDate(), rating: +(1 + rand() * 4).toFixed(1),
      tags: g.join(', '), video: false,
    });
  }

  for (let i = 0; i < 10; i++) {
    const g = sampleN(GENRES, 1);
    add('ReleaseGroup', {
      title: title2(), byArtist: pick(ARTISTS), primaryType: pick(['Album', 'Single', 'EP']),
      datePublished: isoDate(), genres: g, tags: g.join(', '),
      recordings: Array.from({ length: 3 + Math.floor(rand() * 6) }, title2),
    });
  }

  for (let i = 0; i < 12; i++) {
    add('Release', {
      title: title2(), byArtist: pick(ARTISTS), status: pick(['Official', 'Promotion']),
      barcode: String(Math.floor(rand() * 1e12)).padStart(12, '0'), labelName: `${pick(W1)} Records`,
      datePublished: isoDate(), primaryType: pick(['Album', 'Single', 'EP']),
      recordings: Array.from({ length: 4 + Math.floor(rand() * 8) }, title2),
    });
  }

  for (let i = 0; i < 8; i++) {
    add('Place', { title: `${pick(W1)} Studios`, area: pick(AREAS), placeType: pick(['Studio', 'Venue', 'Stadium']),
      address: `${1 + Math.floor(rand() * 200)} ${pick(W2)} Street`, coordinates: `${(rand() * 180 - 90).toFixed(4)}, ${(rand() * 360 - 180).toFixed(4)}`,
      beginYear: year(1950, 2010), text: 'A place where music is made or performed.' });
  }

  for (let i = 0; i < 8; i++) {
    add('Event', { title: `${pick(W2)} Festival ${year(2005, 2024)}`, eventType: pick(['Concert', 'Festival', 'Broadcast']),
      datePublished: isoDate(), time: `${String(18 + Math.floor(rand() * 5)).padStart(2, '0')}:00`, area: pick(AREAS),
      artists: sampleN(ARTISTS, 3 + Math.floor(rand() * 4)), cancelled: false });
  }

  ['Guitar', 'Modular Synth', 'Upright Bass', 'Rhodes Piano', 'Drum Machine', 'Saxophone'].forEach((name) =>
    add('Instrument', { title: name, instrumentType: pick(['String', 'Electronic', 'Keyboard', 'Wind', 'Percussion']),
      text: `The ${name} and its lineage.` }));

  return points;
}

let _data: MockPoint[] | null = null;
const data = (): MockPoint[] => (_data ??= generate());
const stripVector = (p: MockPoint): QdrantPoint => ({ id: p.id, payload: p.payload });

// Atlas: expose the toy document vectors + identity. The mock has no baked
// projection, so the Atlas computes one (PCA) over these synthetic vectors.
export function mockAtlasRaw(): AtlasRaw[] {
  return data().map((p) => ({
    id: String(p.id),
    type: (p.payload?.entityType as string) ?? 'Unknown',
    title: String((p.payload?.fields?.title as string | undefined) ?? p.id),
    vector: p.vector,
    fields: (p.payload?.fields ?? {}) as Record<string, unknown>,
  }));
}

// Atlas: turn a typed free-text query into a vector in the toy space. There is no
// real query model in mock mode, so we tokenize the query and reuse the same
// bag-of-tokens embedding the documents use — token overlap → nearby placement.
export function mockAtlasEmbed(q: string): number[] {
  return embedTokens(q.split(/[^a-z0-9]+/i).filter(Boolean));
}

// Pull the set of entityType values a filter constrains to (the only filter the
// browse path builds). Empty → no type constraint.
function filterTypes(filter?: Filter): Set<string> | null {
  if (!filter?.must) return null;
  const types = new Set<string>();
  for (const clause of filter.must as Array<Record<string, unknown>>) {
    if (clause && clause.key === 'entityType') {
      const m = clause.match as { value?: string; any?: string[] } | undefined;
      if (m?.value) types.add(m.value);
      if (m?.any) m.any.forEach((v) => types.add(v));
    }
  }
  return types.size ? types : null;
}

function paginate(list: QdrantPoint[], limit: number, offset?: string | number | null): ScrollResult {
  const start = Number(offset ?? 0);
  const page = list.slice(start, start + limit);
  const next = start + limit < list.length ? String(start + limit) : null;
  return { points: page, nextOffset: next };
}

// ---- public surface (mirrors qdrant.ts) ----
export function mockCollectionInfo(): Promise<CollectionInfo> {
  return Promise.resolve({ pointsCount: data().length, vectorSize: DIM });
}

export function mockCountByType(type: string): Promise<number> {
  return Promise.resolve(data().filter((p) => p.payload?.entityType === type).length);
}

export function mockCountFiltered(filter?: Filter): Promise<number> {
  const types = filterTypes(filter);
  return Promise.resolve(types ? data().filter((p) => types.has(p.payload?.entityType ?? '')).length : data().length);
}

export function mockScroll(opts: { limit?: number; filter?: Filter; offset?: string | number | null }): Promise<ScrollResult> {
  const types = filterTypes(opts.filter);
  const list = types ? data().filter((p) => types.has(p.payload?.entityType ?? '')) : data();
  return Promise.resolve(paginate(list.map(stripVector), opts.limit ?? 40, opts.offset));
}

export function mockGetPoint(pointId: string): Promise<QdrantPoint> {
  const p = data().find((x) => String(x.id) === pointId);
  if (!p) return Promise.reject(new QdrantError('Not found', 'notfound'));
  return Promise.resolve(stripVector(p));
}

export function mockRecommend(pointId: string, limit = 12): Promise<QdrantPoint[]> {
  const src = data().find((x) => String(x.id) === pointId);
  if (!src) return Promise.resolve([]);
  const scored = data()
    .filter((p) => p.id !== src.id)
    .map((p) => ({ p, score: cosine(src.vector, p.vector) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);
  return Promise.resolve(scored.map(({ p, score }) => ({ ...stripVector(p), score })));
}

// Keyword search: lexical match over the record's text fields, ranked title-first.
// (No query embedding — free-text semantic search is out of scope for the browser
// app; meaning-based discovery happens through `recommend` / "similar entries".)
function lexScore(p: MockPoint, q: string): number {
  const f = p.payload?.fields ?? {};
  const title = String(f.title ?? '').toLowerCase();
  let s = title === q ? 100 : title.startsWith(q) ? 50 : title.includes(q) ? 25 : 0;
  for (const v of [f.byArtist, f.tags, f.area, f.text]) {
    if (typeof v === 'string' && v.toLowerCase().includes(q)) { s += 10; break; }
  }
  for (const k of ['genres', 'moods']) {
    const v = f[k];
    if (Array.isArray(v) && v.some((x) => typeof x === 'string' && x.toLowerCase().includes(q))) { s += 8; break; }
  }
  return s;
}

export function mockSearch(opts: { q: string; type?: string; limit?: number; offset?: string | number | null }): Promise<ScrollResult> {
  const q = opts.q.trim().toLowerCase();
  let list = opts.type ? data().filter((p) => p.payload?.entityType === opts.type) : data();
  if (q) {
    list = list
      .map((p) => ({ p, s: lexScore(p, q) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .map((x) => x.p);
  }
  return Promise.resolve(paginate(list.map(stripVector), opts.limit ?? 20, opts.offset));
}
