// In-browser QUERY embedder. Documents are embedded server-side by the real
// pipeline (quickbeam watch → Qdrant); only the free-text search query needs a
// vector in the browser. We use the SAME model + post-processing as the pipeline
// (quickbeam/embeddings.py: nomic-embed-text-v1.5 → matryoshka(256)) so the query
// vector is directly comparable to the document vectors in the collection. nomic
// is asymmetric, so queries get the "search_query: " prefix.
import type { FeatureExtractionPipeline } from '@huggingface/transformers';

const MODEL = 'nomic-ai/nomic-embed-text-v1.5';
const DIM = 256; // matryoshka slice — must match the collection's vector size

let _extractor: Promise<FeatureExtractionPipeline> | null = null;
function extractor(): Promise<FeatureExtractionPipeline> {
  return (_extractor ??= (async () => {
    const { pipeline } = await import('@huggingface/transformers');
    return pipeline('feature-extraction', MODEL, { dtype: 'q8' });
  })());
}

// Replicates quickbeam/embeddings.py matryoshka(): standardize across the full
// vector, slice to `dim`, L2-normalize the slice.
function matryoshka(vec: Float32Array | number[], dim = DIM): number[] {
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

export async function embedQuery(text: string): Promise<number[]> {
  const ex = await extractor();
  const output = await ex(`search_query: ${text}`, { pooling: 'mean', normalize: false });
  return matryoshka(output.data as Float32Array);
}

// Warm the model (download + init) so the first search isn't a cold stall.
export function warmEmbedder(): Promise<unknown> {
  return extractor();
}
