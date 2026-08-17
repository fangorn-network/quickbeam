// The RELATIONAL axis: load the typed linkset `quickbeam cdn edges` ships at
// /domains/<d>/edges and walk it in-browser — the browser twin of mcp_server.py's
// `neighbors`. This is what makes the patch <-> manual mesh navigable.
import { CDN_URL, CDN_DOMAIN } from './config';
import { getPoint } from './data';
import type { Point } from './types';

export interface Edge { rel: string; from: string; to: string; fromType?: string; toType?: string; }
interface Adj { out: Map<string, Edge[]>; in: Map<string, Edge[]>; }

async function resolveDomain(): Promise<string> {
  if (CDN_DOMAIN) return CDN_DOMAIN;
  const cat = await (await fetch(`${CDN_URL}/catalog`)).json();
  return cat.domains?.[0]?.name;
}

let _edges: Promise<Adj> | null = null;
async function load(): Promise<Adj> {
  const domain = await resolveDomain();
  let list: Edge[] = [];
  try {
    const j = await (await fetch(`${CDN_URL}/domains/${domain}/edges`)).json();
    list = (j.edges ?? j ?? []) as Edge[];
  } catch {
    list = []; // a domain with no linkset just has no mesh
  }
  const out = new Map<string, Edge[]>();
  const inn = new Map<string, Edge[]>();
  for (const e of list) {
    if (!e.from || !e.to || !e.rel) continue;
    (out.get(e.from) ?? out.set(e.from, []).get(e.from)!).push(e);
    (inn.get(e.to) ?? inn.set(e.to, []).get(e.to)!).push(e);
  }
  return { out, in: inn };
}
export function edges(): Promise<Adj> { return (_edges ??= load()); }

function relSet(rel?: string | string[]): Set<string> | null {
  return rel == null ? null : new Set(Array.isArray(rel) ? rel : [rel]);
}

function neighborIds(adj: Adj, id: string, rel: string | string[] | undefined, dir: 'out' | 'in'): string[] {
  const rels = relSet(rel);
  const es = (dir === 'out' ? adj.out.get(id) : adj.in.get(id)) ?? [];
  const ids = es.filter((e) => !rels || rels.has(e.rel)).map((e) => (dir === 'out' ? e.to : e.from));
  return [...new Set(ids)];
}

export async function neighborCount(id: string, rel: string | string[] | undefined, dir: 'out' | 'in'): Promise<number> {
  return neighborIds(await edges(), id, rel, dir).length;
}

// Resolved neighbor points (optionally capped). Missing points (not in the shard) are dropped.
export async function neighbors(
  id: string,
  rel: string | string[] | undefined,
  dir: 'out' | 'in',
  limit?: number,
): Promise<{ points: Point[]; total: number }> {
  const ids = neighborIds(await edges(), id, rel, dir);
  const total = ids.length;
  const slice = limit ? ids.slice(0, limit) : ids;
  const pts = await Promise.all(slice.map(getPoint));
  return { points: pts.filter((p): p is Point => !!p), total };
}
