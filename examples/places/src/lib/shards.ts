// ShardDataSource — the REAL browser data source. Downloads a Semantic CDN snapshot
// (the gzipped NDJSON shards `quickbeam cdn bake` produces) into memory, then serves
// browse / search / count / getPoint / "similar" (cosine) entirely client-side. The
// document vectors are *served* (precomputed by `quickbeam build`); the browser never
// builds embeddings. Same surface as qdrant.ts / mock.ts — swap the impl, keep the
// signatures.
import type { QdrantPoint, EntityFields } from './types';
import type { AtlasRaw } from './atlasTypes';
import type { CollectionInfo, Filter, ScrollResult, StructuredFilters } from './qdrant';
import { QdrantError, sortEvents } from './qdrant';
import { embedQuery } from './embed';
import { parseCoords, haversineKm } from './geo';
import { parseHours, isOpenNow, dateWindowBounds } from './labels';
import type { DomainManifest } from './domain';
import { CDN_URL, CDN_DOMAIN } from './config';

interface ShardPoint extends QdrantPoint {
  vector: number[];
  norm: number;
  // Optional pre-baked 2-D projection (`quickbeam cdn bake` with UMAP). When
  // absent the Atlas projects client-side from `vector`.
  proj?: [number, number];
}

interface Loaded {
  manifest: DomainManifest & { dim?: number; shards?: Array<{ file: string }> };
  points: ShardPoint[];
}

// ---- fetch + gunzip + parse one shard (browser-native DecompressionStream) ----
async function fetchShardRows(url: string): Promise<Array<Record<string, unknown>>> {
  const res = await fetch(url);
  if (!res.ok || !res.body) throw new QdrantError(`shard fetch failed: HTTP ${res.status}`, 'http');
  const stream = res.body.pipeThrough(new DecompressionStream('gzip'));
  const text = await new Response(stream).text();
  return text.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l));
}

function toPoint(row: Record<string, unknown>): ShardPoint | null {
  const embedding = row.embedding as number[] | undefined;
  if (!Array.isArray(embedding)) return null;
  const fields = (row.fields ?? {}) as EntityFields;
  const id = String(row.track_id ?? '');
  let norm = 0;
  for (const x of embedding) norm += x * x;
  // `proj` is the baked 2-D UMAP coordinate, if the snapshot carries one.
  const rawProj = row.proj as unknown;
  const proj =
    Array.isArray(rawProj) && rawProj.length === 2 && rawProj.every((n) => typeof n === 'number')
      ? ([rawProj[0], rawProj[1]] as [number, number])
      : undefined;
  return {
    id,
    vector: embedding,
    norm: Math.sqrt(norm) || 1,
    proj,
    payload: {
      id,
      entityType: (fields.entityType as string) ?? 'Unknown',
      owner: row.owner as string | undefined,
      meta: (row.meta ?? {}) as Record<string, unknown>,
      fields,
    },
  };
}

// Resolve which domain to load: explicit VITE_DOMAIN, else the first in the catalog.
async function resolveDomain(): Promise<string> {
  if (CDN_DOMAIN) return CDN_DOMAIN;
  const res = await fetch(`${CDN_URL}/catalog`);
  if (!res.ok) throw new QdrantError(`CDN catalog failed: HTTP ${res.status}`, 'http');
  const cat = (await res.json()) as { domains?: Array<{ name: string }> };
  const first = cat.domains?.[0]?.name;
  if (!first) throw new QdrantError('CDN catalog has no domains', 'notfound');
  return first;
}

let _loaded: Promise<Loaded> | null = null;
async function load(): Promise<Loaded> {
  const domain = await resolveDomain();
  const mres = await fetch(`${CDN_URL}/domains/${domain}/manifest`);
  if (!mres.ok) throw new QdrantError(`manifest failed: HTTP ${mres.status}`, 'http');
  const manifest = (await mres.json()) as Loaded['manifest'];

  const files = (manifest.shards ?? []).map((s) => s.file);
  // Delta shards re-deliver updated records under the same track_id, so dedupe
  // last-wins (a later shard's row displaces the stale one). Tombstoned ids
  // (delete propagation — shards are immutable) are dropped entirely.
  const byId = new Map<string, ShardPoint>();
  for (const file of files) {
    const rows = await fetchShardRows(`${CDN_URL}/domains/${domain}/shards/${file}`);
    for (const row of rows) {
      const p = toPoint(row);
      if (p) byId.set(p.id, p);
    }
  }
  const tombstones = (manifest as { tombstones?: string[] }).tombstones ?? [];
  for (const id of tombstones) byId.delete(id);
  return { manifest, points: [...byId.values()] };
}
function loaded(): Promise<Loaded> {
  return (_loaded ??= load());
}

// The domain descriptor for lib/domain.ts — the manifest already carries role_map /
// entity_types / bundle / presentation (baked by `cdn bake`), so the client is fully
// self-describing with no inference.
export async function shardManifest(): Promise<DomainManifest> {
  return (await loaded()).manifest;
}

// ---- query helpers ----
function cosine(a: ShardPoint, qv: number[], qn: number): number {
  let d = 0;
  for (let i = 0; i < a.vector.length; i++) d += a.vector[i] * qv[i];
  return d / (a.norm * qn);
}
const strip = (p: ShardPoint): QdrantPoint => ({ id: p.id, payload: p.payload });

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

// Amenities are stored as a JSON-encoded string (e.g. '["live music","dine-in"]').
// Client-side we just parse it — none of Qdrant's keyword-index limitations apply.
function parseAmenities(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (typeof v === 'string' && v.trim().startsWith('[')) {
    try {
      const a = JSON.parse(v);
      return Array.isArray(a) ? a.map(String) : [];
    } catch {
      return [];
    }
  }
  return [];
}

// Apply the structured (payload) filters entirely in-memory. Mirrors the Qdrant
// filter qdrant.ts builds, plus amenities (which Qdrant can't filter server-side).
function matchesFilters(p: ShardPoint, filters?: StructuredFilters): boolean {
  if (!filters) return true;
  const f = p.payload?.fields ?? {};
  if (filters.ratingGte != null && filters.ratingGte > 0) {
    if (!(typeof f.rating === 'number' && f.rating >= filters.ratingGte)) return false;
  }
  if (filters.priceLevels?.length) {
    if (typeof f.priceLevel !== 'string' || !filters.priceLevels.includes(f.priceLevel)) return false;
  }
  if (filters.categories?.length) {
    const cats = Array.isArray(f.categories) ? f.categories.map(String) : [];
    if (!filters.categories.some((c) => cats.includes(c))) return false;
  }
  if (filters.localities?.length) {
    if (typeof f.locality !== 'string' || !filters.localities.includes(f.locality)) return false;
  }
  if (filters.amenities?.length) {
    const am = parseAmenities(f.amenities);
    if (!filters.amenities.some((a) => am.includes(a))) return false;
  }
  // Upcoming-only drops past events; businesses (no isPast) pass through.
  if (filters.upcomingOnly && f.isPast === true) return false;
  // Free-only keeps events explicitly flagged free; field-absent entries pass through.
  if (filters.freeOnly && f.isFree !== true && f.isFree != null) return false;
  // Date window constrains entries that carry a startDate; others pass through.
  if (filters.dateWindow && typeof f.startDate === 'string') {
    const sd = f.startDate.slice(0, 10);
    const { from, to } = dateWindowBounds(filters.dateWindow);
    if (sd < from || sd > to) return false;
  }
  // Open-now constrains entries with parseable hours; others pass through.
  if (filters.openNow) {
    const hoursKey = Object.keys(f).find((k) => /hours/i.test(k) && typeof f[k] === 'string');
    const rows = hoursKey ? parseHours(f[hoursKey]) : null;
    if (rows && isOpenNow(rows) === false) return false;
  }
  return true;
}

// Resolve the active entity-type constraint from either an explicit opts.type or
// a passed-in Qdrant Filter (back-compat), then layer structured filters on top.
function selectPoints(
  points: ShardPoint[],
  opts: { type?: string; filter?: Filter; filters?: StructuredFilters },
): ShardPoint[] {
  const types = opts.type ? new Set([opts.type]) : filterTypes(opts.filter);
  return points.filter(
    (p) =>
      (!types || types.has(p.payload?.entityType ?? '')) && matchesFilters(p, opts.filters),
  );
}

function paginate(list: QdrantPoint[], limit: number, offset?: string | number | null): ScrollResult {
  const start = Number(offset ?? 0);
  const page = list.slice(start, start + limit);
  return { points: page, nextOffset: start + limit < list.length ? String(start + limit) : null };
}

function lexScore(p: ShardPoint, q: string): number {
  const f = p.payload?.fields ?? {};
  const title = String(f.title ?? '').toLowerCase();
  let s = title === q ? 100 : title.startsWith(q) ? 50 : title.includes(q) ? 25 : 0;
  for (const v of [f.byArtist, f.tags, f.area, f.text]) {
    if (typeof v === 'string' && v.toLowerCase().includes(q)) { s += 10; break; }
  }
  return s;
}

// ---- public surface (mirrors qdrant.ts) ----
export async function shardCollectionInfo(): Promise<CollectionInfo> {
  const { manifest, points } = await loaded();
  return { pointsCount: points.length, vectorSize: manifest.dim ?? (points[0]?.vector.length ?? 0) };
}

export async function shardCountByType(type: string): Promise<number> {
  return (await loaded()).points.filter((p) => p.payload?.entityType === type).length;
}

export async function shardCountFiltered(filter?: Filter): Promise<number> {
  const { points } = await loaded();
  return selectPoints(points, { filter }).length;
}

export async function shardScroll(opts: {
  limit?: number;
  filter?: Filter;
  type?: string;
  filters?: StructuredFilters;
  offset?: string | number | null;
}): Promise<ScrollResult> {
  const { points } = await loaded();
  return paginate(selectPoints(points, opts).map(strip), opts.limit ?? 40, opts.offset);
}

export async function shardGetPoint(pointId: string): Promise<QdrantPoint> {
  const p = (await loaded()).points.find((x) => String(x.id) === pointId);
  if (!p) throw new QdrantError('Not found', 'notfound');
  return strip(p);
}

export async function shardRecommend(pointId: string, limit = 12): Promise<QdrantPoint[]> {
  const { points } = await loaded();
  const src = points.find((x) => String(x.id) === pointId);
  if (!src) return [];
  return points
    .filter((p) => p.id !== src.id)
    .map((p) => ({ p, score: cosine(p, src.vector, src.norm) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map(({ p, score }) => ({ ...strip(p), score }));
}

// Score every candidate against the query (semantic cosine; lexical fallback when
// the in-browser embedder is unavailable), returning {point, score} sorted desc.
// Lexical scores are normalized into 0..1 so they merge cleanly with cosine.
async function rankPool(pool: ShardPoint[], q: string): Promise<Array<{ p: ShardPoint; score: number }>> {
  try {
    const qv = await embedQuery(q);
    let qn = 0;
    for (const x of qv) qn += x * x;
    qn = Math.sqrt(qn) || 1;
    return pool
      .map((p) => ({ p, score: cosine(p, qv, qn) }))
      .sort((a, b) => b.score - a.score);
  } catch {
    const ql = q.toLowerCase();
    return pool
      .map((p) => ({ p, score: lexScore(p, ql) / 100 }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score);
  }
}

export async function shardSearch(opts: {
  q: string;
  type?: string;
  filters?: StructuredFilters;
  limit?: number;
  offset?: string | number | null;
}): Promise<ScrollResult> {
  const { points } = await loaded();
  const q = opts.q.trim();
  const limit = opts.limit ?? 20;

  // Child→parent roll-up. When the caller wants Businesses (or hasn't pinned a
  // type), Review documents are allowed to compete in ranking and each Review hit
  // is resolved to its parent Business (Review.fields.businessId === Business
  // .fields.placeId). This is what lets a review's free text ("best tacos in
  // town") surface the *place* itself rather than a standalone review card.
  // Results are deduped to the best-scoring hit per business.
  const rollUp = !opts.type || opts.type === 'Business';

  // Reviews are search-only retrieval keys, never browsable cards: a no-query
  // browse excludes them. (Other types follow the caller's type/structured opts.)
  if (!q) {
    const browse = selectPoints(points, opts).filter((p) => p.payload?.entityType !== 'Review');
    return paginate(browse.map(strip), limit, opts.offset);
  }

  // Candidate pool. Rolling up: Businesses + Reviews compete (structured filters
  // are applied to the *resolved* business below, not the review, so e.g. a 5★
  // review still surfaces a place that itself passes a ratingGte filter). Not
  // rolling up (an explicit non-Business type): the usual type+structured select.
  const pool = rollUp
    ? points.filter((p) => {
        const t = p.payload?.entityType ?? '';
        return t === 'Business' || t === 'Review';
      })
    : selectPoints(points, opts);

  const ranked = await rankPool(pool, q);

  // placeId -> Business, for resolving a Review hit to its venue.
  const bizByPlaceId = new Map<string, ShardPoint>();
  if (rollUp) {
    for (const p of points) {
      if (p.payload?.entityType !== 'Business') continue;
      const pid = (p.payload?.fields as Record<string, unknown> | undefined)?.placeId;
      if (typeof pid === 'string') bizByPlaceId.set(pid, p);
    }
  }

  const seen = new Set<string>();
  const out: QdrantPoint[] = [];
  for (const { p, score } of ranked) {
    let resolved: ShardPoint | undefined = p;
    if (rollUp && p.payload?.entityType === 'Review') {
      const bid = (p.payload?.fields as Record<string, unknown> | undefined)?.businessId;
      resolved = typeof bid === 'string' ? bizByPlaceId.get(bid) : undefined;
      if (!resolved) continue; // orphan review (venue not in this shard) -> drop
    }
    // In roll-up mode every result is a Business; apply structured filters now.
    if (rollUp && !matchesFilters(resolved, opts.filters)) continue;
    const id = String(resolved.id);
    if (seen.has(id)) continue; // keep the best-scoring hit per business
    seen.add(id);
    out.push({ ...strip(resolved), score });
  }
  return paginate(out, limit, opts.offset);
}

// Rank entries by geographic proximity to a "lat,lng" origin. Structured filters
// still apply. Entries without parseable coordinates are dropped.
export async function shardNear(opts: {
  coords: string;
  type?: string;
  filters?: StructuredFilters;
  limit?: number;
  offset?: string | number | null;
}): Promise<ScrollResult> {
  const origin = parseCoords(opts.coords);
  if (!origin) return { points: [], nextOffset: null };
  const { points } = await loaded();
  const ranked = selectPoints(points, opts)
    .map((p) => {
      const c = parseCoords(p.payload?.fields?.coordinates);
      return c ? { p, dist: haversineKm(origin, c) } : null;
    })
    .filter((x): x is { p: ShardPoint; dist: number } => x !== null)
    .sort((a, b) => a.dist - b.dist)
    .map(({ p }) => strip(p));
  return paginate(ranked, opts.limit ?? 20, opts.offset);
}

// The Events a given Business hosts (fields.hostBusinessId === placeId), time-ordered.
export async function shardEventsForHost(placeId: string, limit = 100): Promise<QdrantPoint[]> {
  const { points } = await loaded();
  const out = points
    .filter((p) => p.payload?.entityType === 'Event'
      && (p.payload?.fields as Record<string, unknown> | undefined)?.hostBusinessId === placeId)
    .map(strip);
  return sortEvents(out).slice(0, limit);
}

// Atlas: expose every loaded point's identity + document vector (+ baked proj).
export async function shardAtlasRaw(): Promise<AtlasRaw[]> {
  const { points } = await loaded();
  return points.map((p) => ({
    id: String(p.id),
    type: p.payload?.entityType ?? 'Unknown',
    title: String((p.payload?.fields?.title as string | undefined) ?? p.id),
    vector: p.vector,
    proj: p.proj,
    fields: (p.payload?.fields ?? {}) as Record<string, unknown>,
  }));
}

// Atlas: embed a typed query into the served vector space (real query model).
export async function shardAtlasEmbed(q: string): Promise<number[]> {
  return embedQuery(q);
}

// The Business behind a placeId — to link an Event back to its venue.
export async function shardBusinessByPlaceId(placeId: string): Promise<QdrantPoint | null> {
  const { points } = await loaded();
  const p = points.find((x) => x.payload?.entityType === 'Business'
    && (x.payload?.fields as Record<string, unknown> | undefined)?.placeId === placeId);
  return p ? strip(p) : null;
}
