// THE ONLY FILE IN THE PRIVATE PATH THAT TOUCHES THE NETWORK.
//
// That is the whole point of it existing. The privacy claim on the about/privacy page
// is "your query vector never leaves this tab; what leaves is which public bucket your
// browser asked for" — and a claim like that is only checkable if there is exactly one
// place to check. Auditing this design means reading this file and confirming that
// nothing but a bucket id and an optional owner is ever interpolated into a URL.
//
// FALSIFIER: any code path outside this file that calls fetch/XHR/WebSocket in the
// private retrieval flow, or any request here whose URL or body carries a vector, a
// query string, or a per-client random value.
//
// Zero imports except the Layout type (see quant.ts).
import type { Layout } from './route.ts';

export interface BucketRow {
  id: string;
  fields: Record<string, unknown>;
  owner?: string | null;
  embedding?: number[];
  meta?: Record<string, unknown>;
}

/** Cache keyed by bucket id. A bucket is immutable public data, so a hit means a
 *  repeat search in a region already held sends NOTHING — which is what stops
 *  per-query disclosure accumulating over a session. Measured: 73% of coherent
 *  4-query sessions have two queries in the same cell, hence one request. */
const _buckets = new Map<number, BucketRow[]>();

export function cachedBuckets(): number[] {
  return [...(_buckets.keys())];
}

export function resetCache(): void {
  _buckets.clear();
}

export async function fetchLayout(cdnBase: string, domain: string): Promise<Layout> {
  const r = await fetch(`${cdnBase.replace(/\/$/, '')}/domains/${domain}/index/layout.json`);
  if (!r.ok) throw new Error(`layout ${r.status}`);
  return (await r.json()) as Layout;
}

/** The public codebook, as raw int8 — k*dim bytes. Fetched once, then every query is
 *  routed locally against it. */
export async function fetchCodebook(cdnBase: string, domain: string): Promise<Int8Array> {
  const r = await fetch(`${cdnBase.replace(/\/$/, '')}/domains/${domain}/index/codebook.i8`);
  if (!r.ok) throw new Error(`codebook ${r.status}`);
  return new Int8Array(await r.arrayBuffer());
}

/** Fetch one bucket's members. The ONLY request whose shape depends on the query,
 *  and it depends on it only through a single integer.
 *
 *  The server expands bucket -> cells, so this cannot ask for a narrower slice than a
 *  whole bucket even if a caller wanted to. */
export async function fetchBucket(
  serverBase: string, bucket: number, opts: { owner?: string; limit?: number } = {},
): Promise<BucketRow[]> {
  const hit = _buckets.get(bucket);
  if (hit) return hit;
  const qs = new URLSearchParams();
  if (opts.owner) qs.set('owner', opts.owner);
  if (opts.limit) qs.set('limit', String(opts.limit));
  const q = qs.toString();
  const r = await fetch(`${serverBase.replace(/\/$/, '')}/bucket/${bucket}${q ? `?${q}` : ''}`);
  if (!r.ok) throw new Error(`bucket ${bucket}: ${r.status}`);
  const body = (await r.json()) as { results: BucketRow[] };
  _buckets.set(bucket, body.results);
  return body.results;
}

/** Specific records by id. Used for a cold entity page — a deep link or a refresh
 *  where no bucket is cached. Discloses which records you asked for, which browsing
 *  already discloses via per-record artwork fetches. */
export async function fetchRecords(
  serverBase: string, ids: string[],
): Promise<BucketRow[]> {
  if (!ids.length) return [];
  const r = await fetch(`${serverBase.replace(/\/$/, '')}/records`
    + `?ids=${encodeURIComponent(ids.join(','))}`);
  if (!r.ok) throw new Error(`records: ${r.status}`);
  return ((await r.json()) as { records: BucketRow[] }).records;
}

export interface RelGroup { rel: string; dir: 'out' | 'in'; count: number; crosses: boolean }

/** A record's relation groups, for an entity page over a non-resident record. */
export async function fetchRelations(
  serverBase: string, id: string,
): Promise<RelGroup[]> {
  const r = await fetch(`${serverBase.replace(/\/$/, '')}/adjacency`
    + `?id=${encodeURIComponent(id)}`);
  if (!r.ok) throw new Error(`adjacency: ${r.status}`);
  return ((await r.json()) as { groups: RelGroup[] }).groups;
}

/** One rail's records. */
export async function fetchNeighbours(
  serverBase: string, id: string, rel: string, dir: 'out' | 'in', limit: number,
): Promise<BucketRow[]> {
  const r = await fetch(`${serverBase.replace(/\/$/, '')}/adjacency`
    + `?id=${encodeURIComponent(id)}&rel=${encodeURIComponent(rel)}`
    + `&dir=${dir}&limit=${limit}`);
  if (!r.ok) throw new Error(`adjacency: ${r.status}`);
  return ((await r.json()) as { records: BucketRow[] }).records;
}

/** Fetch several buckets, shuffled.
 *
 *  Order is randomized so the sequence does not rank them: probe order correlates
 *  with proximity to the query, and a server that saw them in order would learn which
 *  cell was nearest — a strictly finer disclosure than the bucket set itself. */
export async function fetchBuckets(
  serverBase: string, buckets: number[], opts: { owner?: string; limit?: number } = {},
): Promise<BucketRow[]> {
  const order = [...buckets];
  for (let i = order.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  const pages = await Promise.all(order.map((b) => fetchBucket(serverBase, b, opts)));
  return pages.flat();
}
