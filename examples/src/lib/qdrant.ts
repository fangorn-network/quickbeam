// Resilient Qdrant REST client. All calls go through the Vite dev proxy at
// /qdrant/* -> http://localhost:6333/*. Designed to never throw raw network
// errors at the UI without a typed wrapper so screens can show inline errors.
//
// Data source selection: with VITE_DATA_SOURCE=qdrant the functions below hit a real
// Qdrant; otherwise (the default) they delegate to the in-memory mock (lib/mock.ts).
// This lets the Tauri desktop build run with no backend, and is the same seam the
// future Rust engine will implement.
import type {
  EntityType,
  EntitySummary,
  QdrantPoint,
  EntityPayload,
} from './types';
import {
  mockCollectionInfo,
  mockCountByType,
  mockCountFiltered,
  mockScroll,
  mockGetPoint,
  mockRecommend,
  mockSearch,
} from './mock';
import {
  shardCollectionInfo,
  shardCountByType,
  shardCountFiltered,
  shardScroll,
  shardGetPoint,
  shardRecommend,
  shardSearch,
  shardNear,
  shardEventsForHost,
  shardBusinessByPlaceId,
} from './shards';
import { IS_MOCK, IS_SHARDS } from './config';
import { embedQuery } from './embed';
import { parseCoords, haversineKm } from './geo';

const BASE = '/qdrant';
// The active collection. Defaults to `fangorn`; a domain manifest can override it
// via `setCollection()` during load (see lib/domain.ts).
let COLLECTION = 'fangorn';

export function setCollection(name: string): void {
  COLLECTION = name;
}

export class QdrantError extends Error {
  constructor(
    message: string,
    public kind: 'network' | 'notfound' | 'http',
  ) {
    super(message);
    this.name = 'QdrantError';
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    });
  } catch {
    throw new QdrantError('Cannot reach Qdrant', 'network');
  }
  if (res.status === 404) throw new QdrantError('Not found', 'notfound');
  if (!res.ok) throw new QdrantError(`HTTP ${res.status}`, 'http');
  return (await res.json()) as T;
}

// ---- Filters ----
export type Filter = { must: unknown[]; must_not?: unknown[] };

export function typeClause(type: EntityType | string) {
  return { key: 'entityType', match: { value: type } };
}

export function textClause(field: string, text: string) {
  return { key: field, match: { text } };
}

export function rangeClause(field: string, gte: number) {
  return { key: field, range: { gte } };
}

export function matchAnyClause(field: string, values: string[]) {
  return { key: field, match: { any: values } };
}

// Structured (payload) filters applied alongside both semantic and browse
// queries. These map to Qdrant payload indexes built by ensure_indexes():
//   ratingGte    -> range on fields.rating (float)
//   priceLevels  -> MatchAny on fields.priceLevel (keyword, e.g. "$", "$$")
//   categories   -> MatchAny on fields.categories (keyword array)
//   amenities    -> client-side only (see below)
// amenities (live music / outdoor seating / …) are stored as an opaque
// JSON-encoded string, so Qdrant's keyword index can't MatchAny on individual
// values — buildFilter() below omits them. The shards data source parses and
// filters them in-memory (see shards.ts), so amenities only filter there.
export interface StructuredFilters {
  ratingGte?: number;
  priceLevels?: string[];
  categories?: string[];
  localities?: string[];
  amenities?: string[];
  // Drop past events (fields.isPast === true). Businesses lack isPast, so they
  // are unaffected — only past Events are excluded.
  upcomingOnly?: boolean;
}

function structuredClauses(filters?: StructuredFilters): unknown[] {
  const must: unknown[] = [];
  if (filters?.ratingGte != null && filters.ratingGte > 0) {
    must.push(rangeClause('fields.rating', filters.ratingGte));
  }
  if (filters?.priceLevels?.length) {
    must.push(matchAnyClause('fields.priceLevel', filters.priceLevels));
  }
  if (filters?.categories?.length) {
    must.push(matchAnyClause('fields.categories', filters.categories));
  }
  if (filters?.localities?.length) {
    must.push(matchAnyClause('fields.locality', filters.localities));
  }
  return must;
}

// Filter for a semantic/browse query: type + structured clauses only. The query
// text drives the vector, not a payload clause.
export function buildFilter(type?: EntityType | string, filters?: StructuredFilters): Filter | undefined {
  const must: unknown[] = [];
  if (type) must.push(typeClause(type));
  must.push(...structuredClauses(filters));
  const mustNot: unknown[] = [];
  if (filters?.upcomingOnly) mustNot.push({ key: 'fields.isPast', match: { value: true } });
  if (!must.length && !mustNot.length) return undefined;
  return { must, ...(mustNot.length ? { must_not: mustNot } : {}) };
}

// Lexical fallback used only when the embedding backend is unreachable: OR text
// match across the two indexed text fields, plus type + structured clauses.
function buildLexicalFilter(
  q: string,
  type?: EntityType | string,
  filters?: StructuredFilters,
): Filter | undefined {
  const must: unknown[] = [];
  if (type) must.push(typeClause(type));
  must.push(...structuredClauses(filters));
  if (q.trim()) {
    must.push({ should: [textClause('fields.title', q), textClause('fields.byArtist', q)] });
  }
  const mustNot: unknown[] = [];
  if (filters?.upcomingOnly) mustNot.push({ key: 'fields.isPast', match: { value: true } });
  if (!must.length && !mustNot.length) return undefined;
  return { must, ...(mustNot.length ? { must_not: mustNot } : {}) };
}

// ---- API ----

export interface CollectionInfo {
  pointsCount: number;
  vectorSize: number;
}

export async function getCollectionInfo(): Promise<CollectionInfo> {
  if (IS_SHARDS) return shardCollectionInfo();
  if (IS_MOCK) return mockCollectionInfo();
  const data = await req<{
    result: {
      points_count: number;
      config?: { params?: { vectors?: { size?: number } } };
    };
  }>(`/collections/${COLLECTION}`);
  return {
    pointsCount: data.result.points_count ?? 0,
    vectorSize: data.result.config?.params?.vectors?.size ?? 0,
  };
}

export async function countByType(type: EntityType | string): Promise<number> {
  if (IS_SHARDS) return shardCountByType(type);
  if (IS_MOCK) return mockCountByType(type);
  const data = await req<{ result: { count: number } }>(
    `/collections/${COLLECTION}/points/count`,
    {
      method: 'POST',
      body: JSON.stringify({ filter: { must: [typeClause(type)] }, exact: true }),
    },
  );
  return data.result.count ?? 0;
}

export async function countFiltered(filter?: Filter): Promise<number> {
  if (IS_SHARDS) return shardCountFiltered(filter);
  if (IS_MOCK) return mockCountFiltered(filter);
  const data = await req<{ result: { count: number } }>(
    `/collections/${COLLECTION}/points/count`,
    {
      method: 'POST',
      body: JSON.stringify(filter ? { filter, exact: true } : { exact: true }),
    },
  );
  return data.result.count ?? 0;
}

export interface ScrollResult {
  points: QdrantPoint[];
  nextOffset: string | number | null;
}

export async function scroll(opts: {
  limit?: number;
  filter?: Filter;
  type?: EntityType | string;
  filters?: StructuredFilters;
  offset?: string | number | null;
}): Promise<ScrollResult> {
  if (IS_SHARDS) return shardScroll(opts);
  if (IS_MOCK) return mockScroll(opts);
  // Accept either a pre-built Qdrant filter or (type + structured filters).
  const filter = opts.filter ?? buildFilter(opts.type, opts.filters);
  const body: Record<string, unknown> = {
    limit: opts.limit ?? 40,
    with_payload: true,
    with_vector: false,
  };
  if (filter) body.filter = filter;
  if (opts.offset != null) body.offset = opts.offset;
  const data = await req<{
    result: { points: QdrantPoint[]; next_page_offset: string | number | null };
  }>(`/collections/${COLLECTION}/points/scroll`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return {
    points: data.result.points ?? [],
    nextOffset: data.result.next_page_offset ?? null,
  };
}

export async function getPoint(pointId: string): Promise<QdrantPoint> {
  if (IS_SHARDS) return shardGetPoint(pointId);
  if (IS_MOCK) return mockGetPoint(pointId);
  const data = await req<{ result: QdrantPoint }>(
    `/collections/${COLLECTION}/points/${encodeURIComponent(pointId)}?with_payload=true`,
  );
  if (!data.result) throw new QdrantError('Not found', 'notfound');
  return data.result;
}

export async function recommend(
  pointId: string,
  limit = 12,
): Promise<QdrantPoint[]> {
  if (IS_SHARDS) return shardRecommend(pointId, limit);
  if (IS_MOCK) return mockRecommend(pointId, limit);
  const data = await req<{ result: QdrantPoint[] }>(
    `/collections/${COLLECTION}/points/recommend`,
    {
      method: 'POST',
      body: JSON.stringify({
        positive: [pointId],
        limit,
        with_payload: true,
      }),
    },
  );
  return data.result ?? [];
}

async function vectorQuery(
  vector: number[],
  filter: Filter | undefined,
  limit: number,
): Promise<ScrollResult> {
  const body: Record<string, unknown> = {
    query: vector,
    limit,
    with_payload: true,
    with_vector: false,
  };
  if (filter) body.filter = filter;
  const data = await req<{ result: { points: QdrantPoint[] } }>(
    `/collections/${COLLECTION}/points/query`,
    { method: 'POST', body: JSON.stringify(body) },
  );
  // Vector search returns a ranked top-N; there is no cursor to page through.
  return { points: data.result.points ?? [], nextOffset: null };
}

export async function search(opts: {
  q: string;
  type?: EntityType | string;
  filters?: StructuredFilters;
  limit?: number;
  offset?: string | number | null;
}): Promise<ScrollResult> {
  if (IS_SHARDS) return shardSearch(opts);
  if (IS_MOCK) return mockSearch(opts);
  const limit = opts.limit ?? 20;
  const q = opts.q.trim();

  // No query text -> structured browse via scroll (keeps cursor pagination).
  if (!q) {
    return scroll({ limit, filter: buildFilter(opts.type, opts.filters), offset: opts.offset });
  }

  // Semantic + structured hybrid: embed the query in-browser, then vector-search
  // with the payload filter applied server-side by Qdrant.
  try {
    const vector = await embedQuery(q);
    return vectorQuery(vector, buildFilter(opts.type, opts.filters), limit);
  } catch {
    // In-browser embedder failed to load -> degrade to lexical scroll so the app
    // still returns results (just without semantic ranking).
    return scroll({
      limit,
      filter: buildLexicalFilter(q, opts.type, opts.filters),
      offset: opts.offset,
    });
  }
}

// Coordinate-proximity search: rank entries by distance from a "lat,lng" origin.
// The string coordinates field isn't a Qdrant geo index, so in qdrant mode we
// rank a (filtered) scroll client-side. Shards mode does it over loaded vectors.
export async function searchNear(opts: {
  coords: string;
  type?: EntityType | string;
  filters?: StructuredFilters;
  limit?: number;
  offset?: string | number | null;
}): Promise<ScrollResult> {
  if (IS_SHARDS) return shardNear(opts);
  if (IS_MOCK) return mockSearch({ q: '', type: opts.type, limit: opts.limit, offset: opts.offset });
  const origin = parseCoords(opts.coords);
  if (!origin) return { points: [], nextOffset: null };
  const all = await scroll({ limit: 1000, type: opts.type, filters: opts.filters });
  const ranked = all.points
    .map((p) => {
      const c = parseCoords((p.payload?.fields as Record<string, unknown> | undefined)?.coordinates);
      return c ? { p, dist: haversineKm(origin, c) } : null;
    })
    .filter((x): x is { p: QdrantPoint; dist: number } => x !== null)
    .sort((a, b) => a.dist - b.dist)
    .slice(0, opts.limit ?? 20)
    .map(({ p }) => p);
  return { points: ranked, nextOffset: null };
}

// ---- Events: time-ordered host lookups ----

// Sort events upcoming-first (soonest first), then past (most recent first).
export function sortEvents(points: QdrantPoint[]): QdrantPoint[] {
  const key = (p: QdrantPoint): [number, string] => {
    const f = (p.payload?.fields ?? {}) as Record<string, unknown>;
    const d = typeof f.startDate === 'string' ? f.startDate : '';
    return [f.isPast === true ? 1 : 0, d];
  };
  return [...points].sort((a, b) => {
    const [pa, da] = key(a);
    const [pb, db] = key(b);
    if (pa !== pb) return pa - pb;
    return pa === 1 ? db.localeCompare(da) : da.localeCompare(db);
  });
}

// The Events a given Business hosts (events_pg stamps fields.hostBusinessId with
// the bar's placeId). Returns live points, so navigation ids are correct in the
// active data source — that's what makes a bar's events clickable.
export async function eventsForHost(placeId: string, limit = 100): Promise<QdrantPoint[]> {
  if (!placeId) return [];
  if (IS_SHARDS) return shardEventsForHost(placeId, limit);
  if (IS_MOCK) return [];
  const filter = {
    must: [typeClause('Event'), { key: 'fields.hostBusinessId', match: { value: placeId } }],
  };
  const data = await req<{ result: { points: QdrantPoint[] } }>(
    `/collections/${COLLECTION}/points/scroll`,
    { method: 'POST', body: JSON.stringify({ limit, with_payload: true, filter }) },
  );
  return sortEvents(data.result.points ?? []);
}

// The Business behind a placeId — used to link an Event back to its venue.
export async function businessByPlaceId(placeId: string): Promise<QdrantPoint | null> {
  if (!placeId) return null;
  if (IS_SHARDS) return shardBusinessByPlaceId(placeId);
  if (IS_MOCK) return null;
  const filter = { must: [{ key: 'fields.placeId', match: { value: placeId } }] };
  const data = await req<{ result: { points: QdrantPoint[] } }>(
    `/collections/${COLLECTION}/points/scroll`,
    { method: 'POST', body: JSON.stringify({ limit: 1, with_payload: true, filter }) },
  );
  return data.result.points?.[0] ?? null;
}

// ---- Mapping ----

export function toSummary(p: QdrantPoint): EntitySummary {
  const payload: EntityPayload = p.payload ?? {};
  const fields = payload.fields ?? {};
  const title =
    (typeof fields.title === 'string' && fields.title) ||
    (typeof payload.id === 'string' && payload.id) ||
    String(p.id);
  return {
    pointId: String(p.id),
    entityType: (payload.entityType as string) ?? 'Unknown',
    title,
    mbid: typeof fields.mbid === 'string' ? fields.mbid : undefined,
    fields,
    score: p.score,
  };
}
