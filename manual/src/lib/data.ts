// The data source: download the baked Semantic-CDN snapshot (gzipped NDJSON shards)
// into memory once, then serve get/search/recommend/byType entirely client-side.
// A condensed cousin of examples/src/lib/shards.ts (document vectors are served;
// only the query is embedded in-browser). Same 256-d matryoshka space as embed.ts.
import { CDN_URL, CDN_DOMAIN } from './config';
import { embedQuery } from './embed';
import type { Fields, Hit, Point } from './types';

export interface Manifest {
  name: string;
  dim?: number;
  role_map?: Record<string, unknown>;
  entity_types?: Array<{ type: string; count: number }>;
  presentation?: { types?: Record<string, { accent?: string; singular?: string; plural?: string; definition?: string }> };
  bundle?: { edges?: Array<{ rel: string; from: string; to: string }> };
  shards?: Array<{ file: string }>;
}

interface Loaded { manifest: Manifest; points: Point[]; byId: Map<string, Point>; }

async function resolveDomain(): Promise<string> {
  if (CDN_DOMAIN) return CDN_DOMAIN;
  const cat = await (await fetch(`${CDN_URL}/catalog`)).json();
  const first = cat.domains?.[0]?.name;
  if (!first) throw new Error('CDN catalog has no domains');
  return first;
}

async function fetchShard(url: string): Promise<Array<Record<string, unknown>>> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`shard fetch failed: HTTP ${res.status}`);
  // The shard is gzipped NDJSON. Some hosts (a static/IPFS host, `quickbeam cdn
  // serve`) send the raw .gz; others (vite's dev server) tag it Content-Encoding:
  // gzip so fetch already decompressed it. Sniff the gzip magic (1f 8b) so we
  // decompress exactly once regardless.
  const buf = new Uint8Array(await res.arrayBuffer());
  const gzipped = buf.length > 1 && buf[0] === 0x1f && buf[1] === 0x8b;
  const text = gzipped
    ? await new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'))).text()
    : new TextDecoder().decode(buf);
  return text.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l));
}

let _loaded: Promise<Loaded> | null = null;
async function load(): Promise<Loaded> {
  const domain = await resolveDomain();
  const manifest = (await (await fetch(`${CDN_URL}/domains/${domain}/manifest`)).json()) as Manifest;
  const byId = new Map<string, Point>();
  for (const s of manifest.shards ?? []) {
    for (const row of await fetchShard(`${CDN_URL}/domains/${domain}/shards/${s.file}`)) {
      const emb = row.embedding as number[] | undefined;
      if (!Array.isArray(emb)) continue;
      const fields = (row.fields ?? {}) as Fields;
      const id = String(row.track_id ?? '');
      let norm = 0;
      for (const x of emb) norm += x * x;
      byId.set(id, {
        id,
        entityType: (fields.entityType as string) ?? 'Unknown',
        fields,
        vector: emb,
        norm: Math.sqrt(norm) || 1,
      });
    }
  }
  return { manifest, points: [...byId.values()], byId };
}
export function data(): Promise<Loaded> {
  return (_loaded ??= load());
}

function cosine(p: Point, qv: number[], qn: number): number {
  let d = 0;
  for (let i = 0; i < p.vector.length; i++) d += p.vector[i] * qv[i];
  return d / (p.norm * qn);
}

export async function manifest(): Promise<Manifest> { return (await data()).manifest; }
export async function allPoints(): Promise<Point[]> { return (await data()).points; }
export async function getPoint(id: string): Promise<Point | null> { return (await data()).byId.get(id) ?? null; }
export async function pointsByType(type: string): Promise<Point[]> {
  return (await data()).points.filter((p) => p.entityType === type);
}

// Per-WORD overlap score (tokenized) so a multi-word query like "warm analog
// lowpass" still returns matches when the embedder is unavailable — the old
// whole-string substring match returned nothing for phrases.
function lex(p: Point, words: string[]): number {
  const title = String(p.fields.title ?? '').toLowerCase();
  const text = String(p.fields.text ?? '').toLowerCase();
  const cat = String(p.fields.category ?? '').toLowerCase();
  let s = 0;
  for (const w of words) {
    if (title === w) s += 1;
    else if (title.includes(w)) s += 0.5;
    if (text.includes(w)) s += 0.2;
    if (cat.includes(w)) s += 0.1;
  }
  return s;
}

export type SearchMode = 'semantic' | 'keyword';
export interface SearchResult { hits: Hit[]; mode: SearchMode; }

// Semantic search (cosine over the served vectors). If the in-browser embedder
// can't load, fall back to keyword overlap and report `mode: 'keyword'` so the UI
// can say so (otherwise the degrade is silent and confusing).
export async function search(q: string, opts: { type?: string; limit?: number } = {}): Promise<SearchResult> {
  const { points } = await data();
  const pool = opts.type ? points.filter((p) => p.entityType === opts.type) : points;
  const limit = opts.limit ?? 40;
  try {
    const qv = await embedQuery(q);
    let qn = 0;
    for (const x of qv) qn += x * x;
    qn = Math.sqrt(qn) || 1;
    const hits = pool
      .map((p) => ({ ...p, score: cosine(p, qv, qn) }))
      .sort((a, b) => b.score - a.score)
      .slice(0, limit);
    return { hits, mode: 'semantic' };
  } catch {
    const words = q.toLowerCase().split(/\s+/).filter((w) => w.length >= 2);
    const hits = pool
      .map((p) => ({ ...p, score: lex(p, words) }))
      .filter((x) => x.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, limit);
    return { hits, mode: 'keyword' };
  }
}

// Semantic neighbours. Sections are excluded: they're the manual's narrative
// headings, already reachable via the TOC, breadcrumbs and prev/next, so
// recommending one is a link the reader already has. Filtered before the slice so
// the limit still yields `limit` results. (Search still returns Sections — that's
// navigation, not recommendation.)
export async function recommend(id: string, limit = 8): Promise<Hit[]> {
  const { points, byId } = await data();
  const src = byId.get(id);
  if (!src) return [];
  return points
    .filter((p) => p.id !== id && p.entityType !== 'Section')
    .map((p) => ({ ...p, score: cosine(p, src.vector, src.norm) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, limit);
}
