// In-browser GENERATIVE model for the /ask concierge. This is distinct from
// lib/embed.ts: that loads an *embedding* model (turns a query into a vector for
// semantic search); this loads a small *instruction-tuned* LLM that (1) turns a
// casual request into an explainable search plan and (2) writes a grounded,
// one-sentence "why this fits" for a result — using only the facts we hand it.
//
// Runs entirely client-side via transformers.js. WebGPU when available (f16,
// fast), else CPU/WASM (q4, slower but works everywhere). The model (~330MB) is
// downloaded once and cached by the browser; warmLLM() lets the UI prefetch it.
import type { EntitySummary } from './types';

// Qwen2.5-0.5B-Instruct: small enough for a first-visit web download, capable
// enough to follow the JSON / single-sentence instructions below.
const MODEL = 'onnx-community/Qwen2.5-0.5B-Instruct';

// ---- load status (the Ask page subscribes to show a download progress bar) ----
export interface LlmStatus {
  stage: 'idle' | 'loading' | 'ready' | 'error';
  progress: number; // 0..1 across the model's files while loading
  message: string;
}
let status: LlmStatus = { stage: 'idle', progress: 0, message: 'Concierge asleep' };
const listeners = new Set<(s: LlmStatus) => void>();
function setStatus(next: Partial<LlmStatus>) {
  status = { ...status, ...next };
  for (const fn of listeners) fn(status);
}
export function getLlmStatus(): LlmStatus {
  return status;
}
export function onLlmStatus(fn: (s: LlmStatus) => void): () => void {
  listeners.add(fn);
  fn(status);
  return () => listeners.delete(fn);
}

// Aggregate per-file download progress into a single 0..1 fraction so the UI can
// show one bar instead of a flurry of file events.
const fileProgress = new Map<string, number>();
function trackProgress(e: { status?: string; file?: string; progress?: number }) {
  if (e.status === 'progress' && e.file && typeof e.progress === 'number') {
    fileProgress.set(e.file, e.progress / 100);
  } else if (e.status === 'done' && e.file) {
    fileProgress.set(e.file, 1);
  }
  if (fileProgress.size) {
    let sum = 0;
    for (const v of fileProgress.values()) sum += v;
    setStatus({ progress: sum / fileProgress.size, message: 'Downloading the concierge…' });
  }
}

// transformers.js text-generation pipeline. Loosely typed: the library's chat
// surface (messages in, messages out) isn't precisely captured by its d.ts here.
type Generator = (
  input: unknown,
  opts: Record<string, unknown>,
) => Promise<Array<{ generated_text: Array<{ role: string; content: string }> }>>;

let _gen: Promise<Generator> | null = null;
function generator(): Promise<Generator> {
  return (_gen ??= (async () => {
    setStatus({ stage: 'loading', progress: 0, message: 'Waking the concierge…' });
    try {
      const { pipeline } = await import('@huggingface/transformers');
      const webgpu = typeof navigator !== 'undefined' && 'gpu' in navigator;
      const gen = (await pipeline('text-generation', MODEL, {
        device: webgpu ? 'webgpu' : 'wasm',
        dtype: webgpu ? 'q4f16' : 'q4',
        progress_callback: trackProgress,
      })) as unknown as Generator;
      setStatus({ stage: 'ready', progress: 1, message: 'Concierge ready' });
      return gen;
    } catch (e) {
      setStatus({ stage: 'error', message: e instanceof Error ? e.message : 'Concierge failed to load' });
      throw e;
    }
  })());
}

// Lets the Ask page kick off the (large) download the moment the user arrives.
export function warmLLM(): void {
  void generator();
}

// Run the chat model to completion and return the assistant's text. `onToken`
// streams partial text for a typewriter reveal.
async function chat(
  system: string,
  user: string,
  maxTokens: number,
  onToken?: (full: string) => void,
): Promise<string> {
  const gen = await generator();
  const opts: Record<string, unknown> = {
    max_new_tokens: maxTokens,
    // Greedy keeps the output grounded, but a 0.5B model decodes into loops
    // ("tacos tacos tacos…") without help. repetition_penalty discourages reusing
    // any prior token; no_repeat_ngram_size HARD-bans repeating any 3-gram, which
    // is what actually breaks the runaway phrase loops.
    do_sample: false,
    repetition_penalty: 1.3,
    no_repeat_ngram_size: 3,
    return_full_text: false,
  };
  if (onToken) {
    const { TextStreamer } = await import('@huggingface/transformers');
    let acc = '';
    // @ts-expect-error — TextStreamer's tokenizer/options typing is loose here.
    opts.streamer = new TextStreamer(gen.tokenizer, {
      skip_prompt: true,
      skip_special_tokens: true,
      callback_function: (t: string) => {
        acc += t;
        onToken(acc.trim());
      },
    });
  }
  const out = await gen(
    [
      { role: 'system', content: system },
      { role: 'user', content: user },
    ],
    opts,
  );
  const msgs = out[0]?.generated_text;
  const last = Array.isArray(msgs) ? msgs[msgs.length - 1] : null;
  return (last?.content ?? '').trim();
}

// ---- planning: casual request -> explainable, searchable intent ----
export interface QueryPlan {
  intent: string; // a short restatement shown to the user ("Group-friendly pizza spot")
  query: string; // a phrase tuned to match place descriptions (fed to semantic search)
  tags: string[]; // 2-4 keyword chips revealing what the concierge keyed on
}

const PLAN_SYSTEM =
  'You are a local discovery concierge. Turn the user\'s casual request into a search plan. ' +
  'Respond with ONLY a JSON object, no prose, of the form ' +
  '{"intent": "<short restatement of what they want>", ' +
  '"query": "<a concise phrase optimized to match a place or event description>", ' +
  '"tags": ["<keyword>", "<keyword>"]}. ' +
  'Keep intent under 6 words. Use 2 to 4 short tags.';

function extractJson(text: string): Record<string, unknown> | null {
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start === -1 || end <= start) return null;
  try {
    return JSON.parse(text.slice(start, end + 1)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

// Plan the query. Always resolves (falls back to the raw query) so the search
// step never blocks on a model hiccup.
export async function planQuery(request: string): Promise<QueryPlan> {
  const fallback: QueryPlan = { intent: request, query: request, tags: [] };
  try {
    const raw = await chat(PLAN_SYSTEM, request, 128);
    const obj = extractJson(raw);
    if (!obj) return fallback;
    const intent = typeof obj.intent === 'string' && obj.intent.trim() ? obj.intent.trim() : request;
    const query = typeof obj.query === 'string' && obj.query.trim() ? obj.query.trim() : request;
    const tags = Array.isArray(obj.tags)
      ? obj.tags.filter((t): t is string => typeof t === 'string' && !!t.trim()).map((t) => t.trim()).slice(0, 4)
      : [];
    return { intent, query, tags };
  } catch {
    return fallback;
  }
}

// ---- grounding: a compact, factual digest of one result for the explainer ----
function parseList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (typeof v === 'string' && v.trim().startsWith('[')) {
    try {
      const a = JSON.parse(v);
      return Array.isArray(a) ? a.map(String) : [];
    } catch {
      return [];
    }
  }
  if (typeof v === 'string' && v.trim()) return [v.trim()];
  return [];
}

function entityDigest(e: EntitySummary): string {
  const f = e.fields as Record<string, unknown>;
  const lines: string[] = [`name: ${e.title}`, `type: ${e.entityType}`];
  const push = (label: string, v: unknown) => {
    if (typeof v === 'string' && v.trim()) lines.push(`${label}: ${v.trim()}`);
    else if (typeof v === 'number') lines.push(`${label}: ${v}`);
  };
  const cats = [...parseList(f.categories), ...parseList(f.tags)];
  if (cats.length) lines.push(`categories: ${[...new Set(cats)].slice(0, 6).join(', ')}`);
  push('rating', f.rating);
  push('price', f.priceLevel);
  const amenities = parseList(f.amenities);
  if (amenities.length) lines.push(`amenities: ${amenities.slice(0, 8).join(', ')}`);
  push('area', (f.locality as string) ?? (f.area as string) ?? (f.venueName as string));
  if (e.entityType === 'Event') {
    push('when', (f.dateLabel as string) ?? (f.startDate as string));
    push('venue', f.venueName);
  }
  const desc = (f.editorialSummary as string) ?? (f.description as string) ?? (f.text as string);
  if (typeof desc === 'string' && desc.trim()) lines.push(`about: ${desc.trim().slice(0, 280)}`);
  return lines.join('\n');
}

const EXPLAIN_SYSTEM =
  'You are a warm, concise local concierge. Given a USER REQUEST and FACTS about one place, ' +
  'write exactly ONE sentence (max 28 words) explaining why it fits the request. ' +
  'Use only the given facts — never invent details, hours, or menu items. ' +
  'Be specific and concrete. Do not start with the place name twice or add quotes.';

// Write the grounded "why it fits" line for one result. Streams via `onToken`.
// Resolves to '' on failure so the card simply renders without a snippet.
export async function explainMatch(
  request: string,
  entity: EntitySummary,
  onToken?: (full: string) => void,
): Promise<string> {
  try {
    const user = `USER REQUEST: ${request}\n\nFACTS:\n${entityDigest(entity)}`;
    const text = await chat(EXPLAIN_SYSTEM, user, 80, onToken);
    // Trim stray wrapping quotes the small model sometimes adds.
    return text.replace(/^["'\s]+|["'\s]+$/g, '');
  } catch {
    return '';
  }
}
