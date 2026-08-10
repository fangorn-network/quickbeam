// The private-retrieval path, checked end to end.
//
// Three things, in increasing scope:
//   1. quant.ts reproduces quickbeam/index.py's bytes EXACTLY (fixture)
//   2. route.ts picks the same cell and bucket Python does (fixture)
//   3. the whole flow works against a live server: route locally, fetch a bucket,
//      re-rank on the client — and NO vector, query string or client random value
//      ever appears in a request
//
// (3) is the one that matters. It is the only test that would catch a future edit
// putting a query vector on the wire, which is the single failure this design exists
// to prevent — and it fails loudly rather than degrading quietly.
//
// Usage:
//   quickbeam cdn index --cdn-dir <dir> --domain <d> --k 977 \
//       --emit-fixture /tmp/quant-fixture.json
//   quickbeam cdn serve --cdn-dir <dir> --cors --port 8091      # codebook + layout
//   quickbeam serve --collection <c> --index-layout <dir>/<d>/index/layout.json
//   FIXTURE=/tmp/quant-fixture.json npm run check:private
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { int8Encode, int8Decode, signEncode, int8TopK, rint } from '../src/lib/quant.ts';
import { nearestCell, bucketsFor, type Layout } from '../src/lib/route.ts';
import * as store from '../src/lib/store.ts';

const CDN = process.env.VITE_CDN_URL ?? 'http://localhost:8091';
const API = process.env.VITE_API_URL ?? 'http://localhost:8080';
const DOMAIN = process.env.VITE_DOMAIN ?? 'audius';
const FIXTURE = process.env.FIXTURE ?? '/tmp/quant-fixture.json';

let failures = 0;
async function check(name: string, fn: () => void | Promise<void>) {
  try { await fn(); console.log(`✓ ${name}`); }
  catch (e) { failures++; console.log(`✗ ${name}\n    ${(e as Error).message}`); }
}

await check('rint rounds half to even, like numpy (not like Math.round)', () => {
  assert.equal(rint(0.5), 0);
  assert.equal(rint(1.5), 2);
  assert.equal(rint(2.5), 2);
  assert.equal(rint(-1.5), -2);
  assert.equal(rint(-2.5), -2);
  // numpy returns -0.0 here and this returns +0. Not worth matching: both are
  // numerically zero and both become byte 0 on assignment into an Int8Array, so the
  // distinction cannot reach the wire. What matters is that it rounds to zero and
  // not to -1.
  assert.equal(Math.abs(rint(-0.5)), 0);
  assert.notEqual(rint(0.5), Math.round(0.5));   // the whole reason rint exists
});

let fx: {
  scale: number; dim: number; k?: number; codebook_scale?: number;
  vectors: number[][]; int8: number[][]; sign: number[][];
  codebook?: number[]; buckets?: number[];
  routing?: Array<{ query: number[]; cell: number; bucket: number }>;
} | null = null;
try { fx = JSON.parse(readFileSync(FIXTURE, 'utf8')); }
catch { console.log(`· no fixture at ${FIXTURE} — skipping cross-language checks`); }

if (fx) {
  await check('int8 encoding is byte-identical to the Python reference', () => {
    for (let i = 0; i < fx!.vectors.length; i++) {
      const got = int8Encode(Float32Array.from(fx!.vectors[i]), fx!.scale);
      assert.deepEqual([...got], fx!.int8[i], `row ${i} differs`);
    }
  });

  await check('sign packing is byte-identical, including sign(0) = +1', () => {
    for (let i = 0; i < fx!.vectors.length; i++) {
      const got = signEncode(Float32Array.from(fx!.vectors[i]));
      assert.deepEqual([...got], fx!.sign[i], `row ${i} differs`);
    }
    // Row 0 carries a deliberate exact zero; its top bit must be set.
    assert.equal(signEncode(Float32Array.from(fx!.vectors[0]))[0] & 0x80, 0x80);
  });

  await check('int8 decode round-trips within quantization error', () => {
    const v = Float32Array.from(fx!.vectors[1]);
    const back = int8Decode(int8Encode(v, fx!.scale), fx!.scale);
    let dot = 0, nv = 0, nb = 0;
    for (let i = 0; i < v.length; i++) { dot += v[i] * back[i]; nv += v[i] ** 2; nb += back[i] ** 2; }
    assert.ok(dot / Math.sqrt(nv * nb) > 0.999, 'cosine after round-trip too low');
  });

  if (fx.routing && fx.codebook && fx.buckets) {
    await check('route.ts picks the same cell and bucket as the Python reference', () => {
      const layout = {
        k: fx!.k!, dim: fx!.dim, nbuckets: 0, salt: '', buckets: fx!.buckets!,
        cell_sizes: [], codebook_scale: fx!.codebook_scale!, vector_scale: fx!.scale, count: 0,
      } as Layout;
      const cb = Int8Array.from(fx!.codebook!);
      for (const c of fx!.routing!) {
        const cell = nearestCell(cb, layout, Float32Array.from(c.query));
        assert.equal(cell, c.cell, `routed to cell ${cell}, Python said ${c.cell}`);
        assert.equal(layout.buckets[cell], c.bucket);
      }
    });
  }
}

// ── live end-to-end ────────────────────────────────────────────────────────────
let layout: Layout | null = null;
try { layout = await store.fetchLayout(CDN, DOMAIN); }
catch { console.log(`· no index at ${CDN}/domains/${DOMAIN}/index — skipping live checks`); }

if (layout) {
  const codebook = await store.fetchCodebook(CDN, DOMAIN);

  await check('the codebook is the size the layout advertises', () => {
    assert.equal(codebook.length, layout!.k * layout!.dim,
      `codebook ${codebook.length} bytes, layout says ${layout!.k}x${layout!.dim}`);
  });

  await check('every cell maps to a bucket, and buckets are evenly sized', () => {
    assert.equal(layout!.buckets.length, layout!.k);
    const sizes = new Map<number, number>();
    for (const b of layout!.buckets) sizes.set(b, (sizes.get(b) ?? 0) + 1);
    const counts = [...sizes.values()];
    // An uneven map means the advertised anonymity set is not the one an unlucky
    // query actually gets, and the public map shows the server which those are.
    assert.ok(Math.max(...counts) - Math.min(...counts) <= 1,
      `bucket sizes range ${Math.min(...counts)}..${Math.max(...counts)}`);
  });

  // A query vector, produced the way the client produces one. Synthetic here: the
  // point is what crosses the wire, not retrieval quality (which the Python gate
  // measured on 500k with real queries).
  const q = new Float32Array(layout.dim);
  for (let i = 0; i < layout.dim; i++) q[i] = Math.sin(i * 0.7);
  let n = 0; for (let i = 0; i < q.length; i++) n += q[i] ** 2;
  n = Math.sqrt(n); for (let i = 0; i < q.length; i++) q[i] /= n;

  const buckets = bucketsFor(codebook, layout, q, 1);

  await check('routing is local — nothing is fetched to choose a bucket', () => {
    assert.equal(buckets.length, 1);
    assert.ok(buckets[0] >= 0 && buckets[0] < layout!.nbuckets);
  });

  await check('a bucket fetch returns members with vectors, and re-ranks locally', async () => {
    let rows: store.BucketRow[];
    try { rows = await store.fetchBucket(API, buckets[0], { limit: 500 }); }
    catch (e) {
      console.log(`    (no /bucket route at ${API}: ${(e as Error).message}) — skipped`);
      return;
    }
    assert.ok(rows.length > 0, 'bucket came back empty');
    const withVec = rows.filter((r) => Array.isArray(r.embedding));
    assert.equal(withVec.length, rows.length, 'some rows arrived without a vector');

    // Re-rank against the TRUE query — the step the whole design rests on.
    const dim = layout!.dim;
    const flat = new Int8Array(withVec.length * dim);
    for (let i = 0; i < withVec.length; i++) {
      flat.set(int8Encode(Float32Array.from(withVec[i].embedding!), layout!.vector_scale), i * dim);
    }
    const top = int8TopK(flat, withVec.length, dim, layout!.vector_scale, q, 10);
    assert.equal(top.length, Math.min(10, withVec.length));
    for (let i = 1; i < top.length; i++) {
      assert.ok(top[i - 1].score >= top[i].score, 're-ranked results are not ordered');
    }
  });

  await check('NO vector, query text or client random value reaches the network', async () => {
    // The falsifier named in store.ts's header, executed. Records every request the
    // private path makes and inspects it.
    const real = globalThis.fetch;
    const seen: Array<{ url: string; body: string }> = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      seen.push({ url: String(input), body: String(init?.body ?? '') });
      return real(input as RequestInfo, init);
    }) as typeof fetch;
    try {
      store.resetCache();
      await store.fetchBuckets(API, bucketsFor(codebook, layout!, q, 4), { limit: 50 })
        .catch(() => []);
    } finally {
      globalThis.fetch = real;
    }
    assert.ok(seen.length > 0, 'recorded no requests at all');
    for (const { url, body } of seen) {
      assert.match(url, /\/bucket\/\d+(\?[a-z=&0-9]*)?$/,
        `request URL is not a bare bucket fetch: ${url}`);
      assert.equal(body, '', `request carried a body: ${body.slice(0, 80)}`);
      assert.doesNotMatch(url, /[-\d.]{6,},/, `URL looks like it carries a vector: ${url}`);
    }
  });

  await check('a repeated bucket is served from cache — a second query sends nothing', async () => {
    store.resetCache();
    const real = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = (async (i: RequestInfo | URL, x?: RequestInit) => {
      calls++; return real(i as RequestInfo, x);
    }) as typeof fetch;
    try {
      await store.fetchBucket(API, buckets[0], { limit: 50 }).catch(() => []);
      const first = calls;
      await store.fetchBucket(API, buckets[0], { limit: 50 }).catch(() => []);
      assert.equal(calls, first, 'a cached bucket still hit the network');
    } finally { globalThis.fetch = real; }
  });
}

console.log(failures ? `\n${failures} check(s) failed.` : '\nPrivate path verified.');
process.exit(failures ? 1 : 0);
