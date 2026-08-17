// Does the browser's ranking agree with the server's?
//
// The client re-implements the query-side vector transform in TypeScript
// (src/lib/matryoshka.ts) to mirror what fastembed + quickbeam/ingest/identity.py did
// to the documents. If those two drift, NOTHING throws — search just quietly starts
// returning worse results, which is the failure mode most likely to survive all the
// way into a live demo.
//
// So: embed a query through the app's own code, score it against the same vectors
// Qdrant holds, and require the top hits to match what Qdrant returns for the same
// query. Run with `npm run check` (needs Qdrant up with the `audius` collection).
import { MATRYOSHKA_DIM, QUERY_PREFIX, matryoshka, topK } from '../src/lib/matryoshka.ts';

const QDRANT = process.env.QDRANT_URL ?? 'http://localhost:6333';
const COLLECTION = process.env.COLLECTION ?? 'audius';
const QUERIES = [
  'dark acid groove house with a moody bassline',
  'uplifting soulful UK garage vocals',
  'Disclosure',
];
const K = 10;

async function qdrant<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${QDRANT}${path}`, {
    method: body ? 'POST' : 'GET',
    headers: { 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`qdrant ${res.status} ${path}: ${await res.text()}`);
  return (await res.json()).result as T;
}

async function main() {
  const { pipeline } = await import('@huggingface/transformers') as unknown as {
    pipeline: (t: string, m: string, o: { dtype: string }) =>
      Promise<(s: string, o: { pooling: 'mean' }) => Promise<{ data: Float32Array }>>;
  };

  console.log('· loading every vector out of Qdrant');
  const ids: string[] = [];
  const vecs: number[][] = [];
  let offset: unknown = undefined;
  for (;;) {
    const page = await qdrant<{ points: Array<{ payload: { id: string }; vector: number[] }>; next_page_offset: unknown }>(
      `/collections/${COLLECTION}/points/scroll`,
      { limit: 4000, offset, with_payload: ['id'], with_vector: true },
    );
    for (const p of page.points) { ids.push(p.payload.id); vecs.push(p.vector); }
    offset = page.next_page_offset;
    if (offset === null || offset === undefined) break;
  }
  console.log(`  ${ids.length} vectors`);

  const flat = new Float32Array(ids.length * MATRYOSHKA_DIM);
  vecs.forEach((v, i) => flat.set(v, i * MATRYOSHKA_DIM));

  console.log('· loading the embedding model');
  const embed = await pipeline('feature-extraction', 'nomic-ai/nomic-embed-text-v1.5', { dtype: 'q8' });

  let failures = 0;
  for (const q of QUERIES) {
    // Client path: the app's OWN prefix + transform, then its own top-k.
    const out = await embed(QUERY_PREFIX + q, { pooling: 'mean' });
    const vec = matryoshka(out.data, MATRYOSHKA_DIM);
    const mine = topK(flat, ids.length, vec, K).map((h) => ids[h.index]);

    // Server path: the same vector, ranked by Qdrant.
    const theirs = (await qdrant<Array<{ payload: { id: string } }>>(
      `/collections/${COLLECTION}/points/search`,
      { vector: Array.from(vec), limit: K, with_payload: ['id'] },
    )).map((p) => p.payload.id);

    const overlap = mine.filter((id) => theirs.includes(id)).length;
    const ok = mine[0] === theirs[0] && overlap >= K - 1;
    console.log(`${ok ? '✓' : '✗'} ${JSON.stringify(q)}  top1 ${mine[0] === theirs[0] ? 'match' : 'MISMATCH'}, ${overlap}/${K} overlap`);
    if (!ok) {
      failures++;
      console.log('   client:', mine.slice(0, 5));
      console.log('   qdrant:', theirs.slice(0, 5));
    }
  }

  if (failures) {
    console.error(`\n${failures} query/document transform mismatch(es) — the client's ` +
                  `matryoshka or prefix has drifted from quickbeam/ingest/identity.py.`);
    process.exit(1);
  }
  console.log('\nClient-side ranking matches Qdrant. The transform is faithful.');
}

main().catch((e) => { console.error(e); process.exit(1); });
