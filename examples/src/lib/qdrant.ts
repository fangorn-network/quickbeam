// Resilient Qdrant REST client. All calls go through the Vite dev proxy at
// /qdrant/* -> http://localhost:6333/*. Designed to never throw raw network
// errors at the UI without a typed wrapper so screens can show inline errors.
import type {
  EntityType,
  EntitySummary,
  QdrantPoint,
  EntityPayload,
} from './types';

const BASE = '/qdrant';
const COLLECTION = 'fangorn';

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
export type Filter = { must: unknown[] };

export function typeClause(type: EntityType | string) {
  return { key: 'entityType', match: { value: type } };
}

export function textClause(field: string, text: string) {
  return { key: field, match: { text } };
}

function buildSearchFilter(q: string, type?: EntityType | string): Filter | undefined {
  const must: unknown[] = [];
  if (type) must.push(typeClause(type));
  if (q.trim()) {
    // OR across the two indexed text fields.
    must.push({
      should: [textClause('fields.title', q), textClause('fields.byArtist', q)],
    });
  }
  return must.length ? { must } : undefined;
}

// ---- API ----

export interface CollectionInfo {
  pointsCount: number;
  vectorSize: number;
}

export async function getCollectionInfo(): Promise<CollectionInfo> {
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
  offset?: string | number | null;
}): Promise<ScrollResult> {
  const body: Record<string, unknown> = {
    limit: opts.limit ?? 40,
    with_payload: true,
    with_vector: false,
  };
  if (opts.filter) body.filter = opts.filter;
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

export async function search(opts: {
  q: string;
  type?: EntityType | string;
  limit?: number;
  offset?: string | number | null;
}): Promise<ScrollResult> {
  return scroll({
    limit: opts.limit ?? 20,
    filter: buildSearchFilter(opts.q, opts.type),
    offset: opts.offset,
  });
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
