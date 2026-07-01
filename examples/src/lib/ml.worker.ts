// Dedicated Web Worker that hosts the transformers.js runtime so ALL in-browser
// inference — the query embedder (feature-extraction) and the concierge LLM
// (text-generation) — runs off the main thread. The model download, WASM/WebGPU
// init, and token-by-token decode therefore never block the UI (previously a hard
// desktop freeze on every search). The main thread drives this via lib/mlWorker.ts
// with id-keyed request/reply; keeping transformers.js here also keeps its ~MB
// runtime out of the main bundle chunk.
import type { FeatureExtractionPipeline } from '@huggingface/transformers';

// Minimal worker-scope typing. We avoid the `webworker` triple-slash lib because
// it redeclares `self` incompatibly with the DOM lib the app tsconfig uses; we
// only need onmessage + postMessage.
const ctx = self as unknown as {
  onmessage: ((e: MessageEvent) => void) | null;
  postMessage: (msg: unknown) => void;
};

const env = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};

// ---- embedder: nomic-embed-text-v1.5, q8 (small download, quality ~unchanged) --
const EMBED_MODEL = 'nomic-ai/nomic-embed-text-v1.5';
let _extractor: Promise<FeatureExtractionPipeline> | null = null;
function extractor(): Promise<FeatureExtractionPipeline> {
  return (_extractor ??= (async () => {
    const { pipeline } = await import('@huggingface/transformers');
    return pipeline('feature-extraction', EMBED_MODEL, { dtype: 'q8' });
  })());
}

// ---- concierge LLM: Qwen2.5-0.5B-Instruct, WebGPU(f16) → WASM(q4) --------------
const CPU_MODEL = 'onnx-community/Qwen2.5-0.5B-Instruct';
const MODEL_OVERRIDE = env.VITE_CONCIERGE_MODEL;
const DTYPE_OVERRIDE = env.VITE_CONCIERGE_DTYPE;

interface LoadConfig {
  model: string;
  device: 'webgpu' | 'wasm';
  dtype: string;
}
function loadChain(webgpu: boolean): LoadConfig[] {
  if (MODEL_OVERRIDE) {
    const device = webgpu ? 'webgpu' : 'wasm';
    return [{ model: MODEL_OVERRIDE, device, dtype: DTYPE_OVERRIDE ?? (webgpu ? 'q4f16' : 'q4') }];
  }
  // 0.5B everywhere: proven-stable and small. The 1.5B is bigger/unstable (fp16
  // garbage on GPU, OOM on mobile), so it's opt-in via VITE_CONCIERGE_MODEL.
  if (webgpu) {
    return [
      { model: CPU_MODEL, device: 'webgpu', dtype: DTYPE_OVERRIDE ?? 'q4f16' },
      { model: CPU_MODEL, device: 'wasm', dtype: 'q4' },
    ];
  }
  return [{ model: CPU_MODEL, device: 'wasm', dtype: DTYPE_OVERRIDE ?? 'q4' }];
}

// `navigator.gpu` can exist while no usable adapter does — probe for a real one so
// we don't route a heavy model onto WASM. navigator is available in workers.
async function detectWebGPU(): Promise<boolean> {
  try {
    const gpu = (navigator as unknown as { gpu?: { requestAdapter(): Promise<unknown> } }).gpu;
    if (!gpu) return false;
    return !!(await gpu.requestAdapter());
  } catch {
    return false;
  }
}

// Aggregate per-file download progress into one 0..1 fraction, posted to the main
// thread as a status message so the UI can show a single bar.
const fileProgress = new Map<string, number>();
function postStatus(stage: LoadStage, progress: number, message: string) {
  ctx.postMessage({ type: 'status', status: { stage, progress, message } });
}
type LoadStage = 'idle' | 'loading' | 'ready' | 'error';
function trackProgress(e: { status?: string; file?: string; progress?: number }) {
  if (e.status === 'progress' && e.file && typeof e.progress === 'number') fileProgress.set(e.file, e.progress / 100);
  else if (e.status === 'done' && e.file) fileProgress.set(e.file, 1);
  if (fileProgress.size) {
    let sum = 0;
    for (const v of fileProgress.values()) sum += v;
    postStatus('loading', sum / fileProgress.size, 'Downloading the concierge…');
  }
}

type Generator = (
  input: unknown,
  opts: Record<string, unknown>,
) => Promise<Array<{ generated_text: Array<{ role: string; content: string }> }>>;

let _gen: Promise<Generator> | null = null;
function generator(): Promise<Generator> {
  return (_gen ??= (async () => {
    postStatus('loading', 0, 'Waking the concierge…');
    const { pipeline } = await import('@huggingface/transformers');
    const webgpu = await detectWebGPU();
    const chain = loadChain(webgpu);
    let lastErr: unknown;
    for (const cfg of chain) {
      fileProgress.clear();
      postStatus('loading', 0, 'Waking the concierge…');
      try {
        const gen = (await pipeline('text-generation', cfg.model, {
          device: cfg.device,
          dtype: cfg.dtype as 'q4',
          progress_callback: trackProgress,
        })) as unknown as Generator;
        postStatus('ready', 1, cfg.model === CPU_MODEL ? 'Concierge ready (lite mode)' : 'Concierge ready');
        return gen;
      } catch (e) {
        lastErr = e;
        console.error(`[concierge] load failed (${cfg.model} on ${cfg.device}/${cfg.dtype}):`, e);
      }
    }
    postStatus('error', 0, lastErr instanceof Error ? lastErr.message : 'Concierge failed to load');
    throw lastErr;
  })());
}

// Greedy decode with repetition guards (a 0.5B loops without them); return the
// assistant's final text. Runs to completion here in the worker.
async function generate(system: string, user: string, maxTokens: number): Promise<string> {
  const gen = await generator();
  const out = await gen(
    [
      { role: 'system', content: system },
      { role: 'user', content: user },
    ],
    {
      max_new_tokens: maxTokens,
      do_sample: false,
      repetition_penalty: 1.3,
      no_repeat_ngram_size: 3,
      return_full_text: false,
    },
  );
  const msgs = out[0]?.generated_text;
  const last = Array.isArray(msgs) ? msgs[msgs.length - 1] : null;
  return (last?.content ?? '').trim();
}

ctx.onmessage = async (e: MessageEvent) => {
  const { id, kind, payload } = e.data as {
    id: number;
    kind: 'embed' | 'generate' | 'warm';
    payload: { text?: string; system?: string; user?: string; maxTokens?: number; what?: 'embed' | 'llm' };
  };
  try {
    let result: unknown;
    if (kind === 'embed') {
      const ex = await extractor();
      const output = await ex(payload.text ?? '', { pooling: 'mean', normalize: false });
      result = Array.from(output.data as Float32Array);
    } else if (kind === 'generate') {
      result = await generate(payload.system ?? '', payload.user ?? '', payload.maxTokens ?? 64);
    } else {
      if (payload.what === 'embed') await extractor();
      else await generator();
      result = true;
    }
    ctx.postMessage({ id, ok: true, result });
  } catch (err) {
    ctx.postMessage({ id, ok: false, error: err instanceof Error ? err.message : String(err) });
  }
};
