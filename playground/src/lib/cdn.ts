// Semantic-CDN read client — the browser data source for search. Instead of hitting
// Qdrant, we download the gzipped NDJSON shards that `quickbeam cdn bake` produces
// and `quickbeam cdn serve` serves (proxied at /cdn), and rank them in-browser by
// cosine against the query vector. Document vectors are *served* (precomputed by the
// pipeline); the browser only embeds the query. This mirrors examples/src/lib/shards.ts,
// pared down to just what search needs.
import { embedQuery } from './embed';
import type { SearchHit } from './types';

const CDN_DOMAIN = (import.meta.env.VITE_CDN_DOMAIN as string) ?? '';

interface CdnPoint {
  id: string;
  vector: number[];
  norm: number;
  payload: { entityType: string; [k: string]: unknown };
}

interface Loaded {
  domain: string;
  dim: number;
  points: CdnPoint[];
}

async function fetchShardRows(url: string): Promise<Array<Record<string, unknown>>> {
  const res = await fetch(url);
  if (!res.ok || !res.body) throw new Error(`shard fetch failed: HTTP ${res.status}`);
  // Shards are gzipped NDJSON — decompress with the browser-native stream.
  const stream = res.body.pipeThrough(new DecompressionStream('gzip'));
  const text = await new Response(stream).text();
  return text.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l));
}

function toPoint(row: Record<string, unknown>): CdnPoint | null {
  const embedding = row.embedding as number[] | undefined;
  if (!Array.isArray(embedding)) return null;
  const fields = (row.fields ?? {}) as Record<string, unknown>;
  const id = String(row.track_id ?? '');
  let norm = 0;
  for (const x of embedding) norm += x * x;
  return {
    id,
    vector: embedding,
    norm: Math.sqrt(norm) || 1,
    payload: { entityType: (fields.entityType as string) ?? 'Unknown', ...fields },
  };
}

async function resolveDomain(): Promise<string> {
  if (CDN_DOMAIN) return CDN_DOMAIN;
  const res = await fetch('/cdn/catalog');
  if (!res.ok) throw new Error(`CDN catalog failed: HTTP ${res.status} — is 'quickbeam cdn serve' running?`);
  const cat = (await res.json()) as { domains?: Array<{ name: string }> };
  const first = cat.domains?.[0]?.name;
  if (!first) throw new Error('CDN catalog has no domains — bake one with `quickbeam cdn bake`.');
  return first;
}

let _loaded: Promise<Loaded> | null = null;
async function load(): Promise<Loaded> {
  const domain = await resolveDomain();
  const mres = await fetch(`/cdn/domains/${domain}/manifest`);
  if (!mres.ok) throw new Error(`manifest failed: HTTP ${mres.status}`);
  const manifest = (await mres.json()) as {
    dim?: number;
    shards?: Array<{ file: string }>;
    tombstones?: string[];
  };

  // Delta shards re-deliver updated records under the same track_id — dedupe
  // last-wins; tombstoned ids (delete propagation) are dropped entirely.
  const byId = new Map<string, CdnPoint>();
  for (const s of manifest.shards ?? []) {
    const rows = await fetchShardRows(`/cdn/domains/${domain}/shards/${s.file}`);
    for (const row of rows) {
      const p = toPoint(row);
      if (p) byId.set(p.id, p);
    }
  }
  for (const id of manifest.tombstones ?? []) byId.delete(id);
  const points = [...byId.values()];
  return { domain, dim: manifest.dim ?? points[0]?.vector.length ?? 0, points };
}
function loaded(): Promise<Loaded> {
  return (_loaded ??= load());
}

// Re-download the snapshot (after publishing + re-baking, new shards appear).
export function reloadCdn(): void {
  _loaded = null;
}

export async function cdnInfo(): Promise<{ domain: string; points: number; dim: number }> {
  const l = await loaded();
  return { domain: l.domain, points: l.points.length, dim: l.dim };
}

export async function cdnSearch(query: string, limit = 12): Promise<SearchHit[]> {
  const { points } = await loaded();
  const qv = await embedQuery(query);
  let qn = 0;
  for (const x of qv) qn += x * x;
  qn = Math.sqrt(qn) || 1;
  return points
    .map((p) => {
      let d = 0;
      for (let i = 0; i < p.vector.length; i++) d += p.vector[i] * qv[i];
      return { id: p.id, score: d / (p.norm * qn), payload: p.payload };
    })
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);
}
