// The private retrieval path, end to end, from a real typed query.
//
// This is the flow a client would run, with nothing faked: embed locally, route
// locally against the public codebook, disclose ONE integer, fetch that bucket, and
// re-rank the candidates against the true query in-process.
//
// It prints what was disclosed alongside the results, because the point of the design
// is not that it retrieves — it is that retrieving cost you a bucket id and nothing
// else. If the "sent to server" line ever shows more than bucket ids, the design is
// broken and this is where you would see it.
//
//   quickbeam cdn serve --cdn-dir <dir> --cors --port 8091
//   quickbeam serve --collection <c> --index-layout <dir>/<domain>/index/layout.json
//   npm run private-search -- "dark acid groove house with a moody bassline"
import { int8Encode, int8TopK } from '../src/lib/quant.ts';
import { bucketsFor, nearestCells, type Layout } from '../src/lib/route.ts';
import * as store from '../src/lib/store.ts';
import { matryoshka, MATRYOSHKA_DIM, QUERY_PREFIX } from '../src/lib/matryoshka.ts';

const CDN = process.env.VITE_CDN_URL ?? 'http://localhost:8091';
const API = process.env.VITE_API_URL ?? 'http://localhost:8080';
const DOMAIN = process.env.VITE_DOMAIN ?? 'audius';
const NPROBE = Number(process.env.NPROBE ?? 1);
const text = process.argv.slice(2).join(' ').trim();

if (!text) {
  console.error('usage: npm run private-search -- "<your query>"');
  process.exit(2);
}

// Record every request so the disclosure can be printed rather than asserted.
const wire: string[] = [];
const realFetch = globalThis.fetch;
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  wire.push(String(input));
  return realFetch(input as RequestInfo, init);
}) as typeof fetch;

console.log(`query: ${JSON.stringify(text)}`);

// 1. Embed locally. Same model, prefix and transform as the browser worker.
const mod = await import('@huggingface/transformers');
const pipe = await (mod as unknown as {
  pipeline: (t: string, m: string, o: { dtype: string }) => Promise<
    (s: string, o: { pooling: string }) => Promise<{ data: Float32Array }>>;
}).pipeline('feature-extraction', 'nomic-ai/nomic-embed-text-v1.5', { dtype: 'q8' });
const raw = await pipe(QUERY_PREFIX + text, { pooling: 'mean' });
const q = matryoshka(raw.data, MATRYOSHKA_DIM);
const embedRequests = wire.length;   // model weights only; no query left the process

// 2. Route locally against the public codebook.
const layout: Layout = await store.fetchLayout(CDN, DOMAIN);
const codebook = await store.fetchCodebook(CDN, DOMAIN);
const cells = nearestCells(codebook, layout, q, NPROBE);
const buckets = bucketsFor(codebook, layout, q, NPROBE);
const beforeBuckets = wire.length;

// 3. Fetch those buckets. The ONLY query-dependent request.
const rows = await store.fetchBuckets(API, buckets, {});
const withVec = rows.filter((r) => Array.isArray(r.embedding));

// 4. Re-rank against the TRUE query.
const dim = layout.dim;
const flat = new Int8Array(withVec.length * dim);
for (let i = 0; i < withVec.length; i++) {
  flat.set(int8Encode(Float32Array.from(withVec[i].embedding!), layout.vector_scale), i * dim);
}
const top = int8TopK(flat, withVec.length, dim, layout.vector_scale, q, 10);

globalThis.fetch = realFetch;

const bucketSize = layout.buckets.filter((b) => b === buckets[0]).length;
console.log(`\nrouted locally  -> cell(s) ${cells.join(', ')} of ${layout.k}`);
console.log(`SENT TO SERVER  -> bucket(s) ${buckets.join(', ')} `
  + `(each hides your cell among ${bucketSize} unrelated ones)`);
console.log(`received        -> ${rows.length} candidates, re-ranked here against the true query`);
console.log(`\nrequests made after the query was embedded:`);
for (const u of wire.slice(embedRequests)) {
  console.log(`  ${u}${wire.indexOf(u) >= beforeBuckets ? '   <- the only query-dependent one' : ''}`);
}

console.log('\ntop 10:');
for (const { index, score } of top) {
  const f = withVec[index].fields as { title?: string; artist?: string; genre?: string };
  console.log(`  ${score.toFixed(3)}  ${f.title ?? '(untitled)'}`
    + `${f.artist ? ` — ${f.artist}` : ''}${f.genre ? `  [${f.genre}]` : ''}`);
}
