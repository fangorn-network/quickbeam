// In-browser query embedder. The transformers.js model now runs in the shared ML
// worker (lib/ml.worker.ts) so it never blocks the UI thread; this module owns
// only the nomic-specific query convention and the matryoshka post-processing that
// keeps the query vector aligned with the document vectors baked by
// quickbeam/embeddings.py.
//
// Document side (quickbeam/embeddings.py): fastembed nomic-embed-text-v1.5 on
//   "search_document: " + text, then matryoshka(vec, 256) =
//   layer-norm over the full 768 dims -> slice to 256 -> L2-normalize.
// Query side (here): same model on "search_query: " + text (nomic is asymmetric),
//   then the identical matryoshka. Layer-norm is scale-invariant, so it does not
//   matter that transformers.js mean-pools without L2-normalizing first.
import { workerEmbed, workerWarm } from './mlWorker';

const MATRYOSHKA_DIM = 256; // must match the collection / shard vector dim

// Replicates quickbeam/embeddings.py `matryoshka()` exactly: standardize across
// the full vector, slice to dim, then L2-normalize the slice.
function matryoshka(vec: number[], dim = MATRYOSHKA_DIM): number[] {
  const n = vec.length;
  let mean = 0;
  for (let i = 0; i < n; i++) mean += vec[i];
  mean /= n;
  let varsum = 0;
  for (let i = 0; i < n; i++) {
    const d = vec[i] - mean;
    varsum += d * d;
  }
  const std = Math.sqrt(varsum / n + 1e-5);

  const out = new Array<number>(Math.min(dim, n));
  for (let i = 0; i < out.length; i++) out[i] = (vec[i] - mean) / std;

  let norm = 0;
  for (const x of out) norm += x * x;
  norm = Math.sqrt(norm);
  if (norm) for (let i = 0; i < out.length; i++) out[i] /= norm;
  return out;
}

// Embed a free-text query into a 256-d, L2-normalized vector aligned with the
// document vectors. The model forward pass happens in the worker; the first call
// downloads/initializes the model there (slow), cached thereafter. Rejects if the
// model can't load — callers fall back to lexical.
export async function embedQuery(text: string): Promise<number[]> {
  const raw = await workerEmbed(`search_query: ${text}`);
  return matryoshka(raw);
}

// Lets the UI warm the model up (in the worker) before the first query. Failures
// are swallowed inside the worker bridge, so warm-up never throws here.
export function warmEmbedder(): void {
  workerWarm('embed');
}
