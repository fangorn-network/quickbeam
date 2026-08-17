// In-browser query embedder (copied from examples/src/lib/embed.ts). transformers.js,
// nomic-embed-text-v1.5 on "search_query: " + text, matryoshka -> 256, matching the
// document side (quickbeam prebake --dim 256) so cosine in data.ts is meaningful.
import type { FeatureExtractionPipeline } from '@huggingface/transformers';

const MODEL = 'nomic-ai/nomic-embed-text-v1.5';
const MATRYOSHKA_DIM = 256; // must match the baked shard vector dim

let _extractor: Promise<FeatureExtractionPipeline> | null = null;
function extractor(): Promise<FeatureExtractionPipeline> {
  return (_extractor ??= (async () => {
    const { pipeline } = await import('@huggingface/transformers');
    return pipeline('feature-extraction', MODEL, { dtype: 'q8' });
  })());
}

// Standardize across the full vector, slice to dim, then L2-normalize (== quickbeam
// embeddings.py matryoshka()).
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

export async function embedQuery(text: string): Promise<number[]> {
  const ex = await extractor();
  const output = await ex(`search_query: ${text}`, { pooling: 'mean', normalize: false });
  return matryoshka(output.data as Float32Array);
}

export function warmEmbedder(): void {
  extractor().catch(() => {/* falls back to lexical on first real query */});
}
