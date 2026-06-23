// ShardDataSource — the REAL browser data source. Downloads a Semantic CDN snapshot
// (the gzipped NDJSON shards `quickbeam cdn bake` produces) into memory, then serves
// browse / search / count / getPoint / "similar" (cosine) entirely client-side. The
// document vectors are *served* (precomputed by `quickbeam build`); the browser never
// builds embeddings. Same surface as qdrant.ts / mock.ts — swap the impl, keep the
// signatures.
import type { QdrantPoint, EntityFields } from './types';
import type { CollectionInfo, Filter, ScrollResult } from './qdrant';
import { QdrantError } from './qdrant';
import type { DomainManifest } from './domain';
import { CDN_URL, CDN_DOMAIN } from './config';

interface ShardPoint extends QdrantPoint {
  vector: number[];
  norm: number;
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
  return {
    id,
    vector: embedding,
    norm: Math.sqrt(norm) || 1,
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
  const points: ShardPoint[] = [];
  for (const file of files) {
    const rows = await fetchShardRows(`${CDN_URL}/domains/${domain}/shards/${file}`);
    for (const row of rows) {
      const p = toPoint(row);
      if (p) points.push(p);
    }
  }
  return { manifest, points };
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
  const types = filterTypes(filter);
  return types ? points.filter((p) => types.has(p.payload?.entityType ?? '')).length : points.length;
}

export async function shardScroll(opts: { limit?: number; filter?: Filter; offset?: string | number | null }): Promise<ScrollResult> {
  const { points } = await loaded();
  const types = filterTypes(opts.filter);
  const list = types ? points.filter((p) => types.has(p.payload?.entityType ?? '')) : points;
  return paginate(list.map(strip), opts.limit ?? 40, opts.offset);
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

export async function shardSearch(opts: { q: string; type?: string; limit?: number; offset?: string | number | null }): Promise<ScrollResult> {
  const { points } = await loaded();
  const q = opts.q.trim().toLowerCase();
  let list = opts.type ? points.filter((p) => p.payload?.entityType === opts.type) : points;
  if (q) {
    list = list
      .map((p) => ({ p, s: lexScore(p, q) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .map((x) => x.p);
  }
  return paginate(list.map(strip), opts.limit ?? 20, opts.offset);
}
