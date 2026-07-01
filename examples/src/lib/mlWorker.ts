// Main-thread bridge to the ML worker (lib/ml.worker.ts), which owns the
// transformers.js runtime for BOTH the query embedder (feature-extraction) and
// the concierge LLM (text-generation). This module spins the worker up lazily,
// does id-keyed request/reply, and relays the LLM download-progress status so the
// UI can show a bar. One worker means the two models share a single transformers.js
// instance and — crucially — neither the ~330MB model load nor token decoding ever
// runs on the UI thread (the freeze this replaces).

export interface LlmStatus {
  stage: 'idle' | 'loading' | 'ready' | 'error';
  progress: number; // 0..1 across the model's files while loading
  message: string;
}

type Pending = { resolve: (v: unknown) => void; reject: (e: unknown) => void };
type WorkerReply =
  | { type: 'status'; status: LlmStatus }
  | { id: number; ok: true; result: unknown }
  | { id: number; ok: false; error: string };

let worker: Worker | null = null;
let seq = 0;
const pending = new Map<number, Pending>();

let status: LlmStatus = { stage: 'idle', progress: 0, message: 'Concierge asleep' };
const statusListeners = new Set<(s: LlmStatus) => void>();

function ensureWorker(): Worker {
  if (worker) return worker;
  // Vite resolves this URL form to a bundled, code-split worker chunk; { type:
  // 'module' } lets the worker use ESM imports (transformers.js) directly.
  const w = new Worker(new URL('./ml.worker.ts', import.meta.url), { type: 'module' });
  w.onmessage = (e: MessageEvent) => {
    const msg = e.data as WorkerReply;
    if ('type' in msg && msg.type === 'status') {
      status = msg.status;
      for (const fn of statusListeners) fn(status);
      return;
    }
    if (!('id' in msg)) return;
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.ok) p.resolve(msg.result);
    else p.reject(new Error(msg.error));
  };
  w.onerror = (e) => {
    // A worker-level failure (module load, OOM) rejects everything in flight so
    // callers fall back (lexical search / rule-based intent) instead of hanging on
    // a promise that will never settle.
    const err = new Error(e.message || 'ML worker error');
    for (const [, p] of pending) p.reject(err);
    pending.clear();
  };
  worker = w;
  return w;
}

function call<T>(kind: 'embed' | 'generate' | 'warm', payload: unknown): Promise<T> {
  const w = ensureWorker();
  const id = ++seq;
  return new Promise<T>((resolve, reject) => {
    pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
    w.postMessage({ id, kind, payload });
  });
}

// Embed a (prefixed) query string → the model's raw mean-pooled vector. The
// nomic "search_query:" convention and matryoshka post-processing stay in embed.ts.
export function workerEmbed(text: string): Promise<number[]> {
  return call<number[]>('embed', { text });
}

// Run the concierge chat model to completion → the assistant's text.
export function workerGenerate(system: string, user: string, maxTokens: number): Promise<string> {
  return call<string>('generate', { system, user, maxTokens });
}

// Preload a model without running inference (UI warm-up). Swallows failures — a
// warm-up must never surface as an unhandled rejection; the real call retries/falls back.
export function workerWarm(what: 'embed' | 'llm'): void {
  void call('warm', { what }).catch(() => {});
}

export function getLlmStatus(): LlmStatus {
  return status;
}

export function onLlmStatus(fn: (s: LlmStatus) => void): () => void {
  statusListeners.add(fn);
  fn(status);
  return () => statusListeners.delete(fn);
}
