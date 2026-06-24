// In-browser query embedder. The whole point of the shards data source is that
// the demo runs with no backend — but free-text semantic search needs the typed
// query turned into a vector. We do that here with transformers.js (WASM, falls
// back from WebGPU), using the SAME model and post-processing as the document
// side so cosine in shards.ts / the vector query in qdrant.ts is meaningful.
//
// Document side (quickbeam/embeddings.py): fastembed nomic-embed-text-v1.5 on
//   "search_document: " + text, then matryoshka(vec, 256) =
//   layer-norm over the full 768 dims -> slice to 256 -> L2-normalize.
// Query side (here): same model on "search_query: " + text (nomic is asymmetric),
//   then the identical matryoshka. Layer-norm is scale-invariant, so it does not
//   matter that transformers.js mean-pools without L2-normalizing first.
// Type-only import (erased at build) so the ~MB transformers.js runtime stays out
// of the main bundle — it's pulled via dynamic import() on first use below.
import type { FeatureExtractionPipeline } from '@huggingface/transformers';

const MODEL = 'nomic-ai/nomic-embed-text-v1.5';
const MATRYOSHKA_DIM = 256; // must match the collection / shard vector dim

let _extractor: Promise<FeatureExtractionPipeline> | null = null;
function extractor(): Promise<FeatureExtractionPipeline> {
  // Lazy-load transformers.js itself, then the model (q8 keeps the download small;
  // quality is essentially unchanged at this dim). Both are cached after first use.
  return (_extractor ??= (async () => {
    const { pipeline } = await import('@huggingface/transformers');
    return pipeline('feature-extraction', MODEL, { dtype: 'q8' });
  })());
}

// Replicates quickbeam/embeddings.py `matryoshka()` exactly: standardize across
// the full vector, slice to dim, then L2-normalize the slice.
function matryoshka(vec: Float32Array | number[], dim = MATRYOSHKA_DIM): number[] {
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
// document vectors. First call downloads/initializes the model (slow); cached
// thereafter. Throws if the model can't load — callers fall back to lexical.
export async function embedQuery(text: string): Promise<number[]> {
  const ex = await extractor();
  const output = await ex(`search_query: ${text}`, { pooling: 'mean', normalize: false });
  return matryoshka(output.data as Float32Array);
}

// Lets the UI warm the model up (and show a spinner) before the first query.
export function warmEmbedder(): void {
  void extractor();
}
