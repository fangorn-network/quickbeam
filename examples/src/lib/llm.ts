// Concierge logic for the discovery surface: (1) turn a casual request into
// structured filters, (2) re-rank semantic candidates by true fit, and (3) write a
// grounded "why it fits" line. The generative model (~330MB Qwen2.5-0.5B) and the
// transformers.js runtime now live in a dedicated Web Worker (lib/ml.worker.ts) so
// token decoding never blocks the UI thread — see lib/mlWorker.ts for the bridge.
// This module keeps only the MAIN-THREAD concerns: the capability gate (navigator /
// matchMedia, which workers can't see), prompt construction, and output parsing.
import type { EntitySummary } from './types';
import { intentFromLlmJson, LLM_AMENITY_TOKENS, type LlmIntent } from './queryParse';
import { workerGenerate, workerWarm, getLlmStatus, onLlmStatus, type LlmStatus } from './mlWorker';

// The download/status API is served by the worker bridge; re-export so existing
// importers (progress bar) keep their import path.
export type { LlmStatus };
export { getLlmStatus, onLlmStatus };

const llmEnv = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};

// Lets the UI kick off the (large) model download the moment it's wanted.
export function warmLLM(): void {
  workerWarm('llm');
}

// Run the chat model to completion and return the assistant's text. Generation
// happens in the worker (off the UI thread). `onToken` is kept for API compat with
// the retired Ask page's typewriter reveal; since we no longer stream token-by-token
// it fires once with the final text.
async function chat(
  system: string,
  user: string,
  maxTokens: number,
  onToken?: (full: string) => void,
): Promise<string> {
  const text = await workerGenerate(system, user, maxTokens);
  if (onToken) onToken(text);
  return text;
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

// ---- intent: refine a query into structured filters (augments queryParse) ----
// The tiny model used where it's actually strong: not writing prose, but making a
// fast, BOUNDED classification. It picks from a closed menu of the same constraints
// the regexes know, so a wrong guess is cheap (semantic search still runs) and the
// output is always something the filter pipeline already enforces.
const INTERPRET_SYSTEM =
  'You convert a local-discovery search query into structured filters. ' +
  'Respond with ONLY a JSON object, no prose. Include a field ONLY when the query ' +
  'clearly implies it — never guess. Fields:\n' +
  '"price": "cheap" or "upscale";\n' +
  '"openNow": true (when they want somewhere open right now);\n' +
  '"topRated": true (best / top-rated / highly reviewed);\n' +
  '"gems": true (hidden gems / underrated / off the beaten path);\n' +
  '"amenities": an array choosing ONLY from this exact list: ' +
  LLM_AMENITY_TOKENS.join(', ') +
  '.\nDo not invent fields, values, or amenities. If nothing clearly applies, output {}.';

// Whether to attempt the in-browser generative model at all. It's a ~300MB+
// download and decodes heavily — fine on a laptop, hostile on a phone or a metered
// connection. We gate on Save-Data, slow/effective connection type, low device
// memory, and a coarse mobile UA check. The refine is a commodity nicety, so when
// in doubt we skip it and keep the (free, instant) rule-based interpretation.
// Set VITE_FORCE_CONCIERGE=1 to bypass the heuristic (useful in mobile emulation,
// where the spoofed UA would otherwise disable the model on a perfectly capable
// laptop). Returns the gate decision AND a reason, so callers can log why they skip.
const FORCE_CONCIERGE = llmEnv.VITE_FORCE_CONCIERGE === '1';

// A real, battery-constrained mobile device — running an in-browser LLM here pegs
// the CPU/GPU and cooks the phone, so we HARD-disable the concierge regardless of
// VITE_FORCE_CONCIERGE (the force flag is only meant to defeat the softer desktop
// heuristics during laptop emulation, never to run the model on an actual phone).
// We require both a mobile UA *and* a narrow, coarse-pointer viewport so a desktop
// emulating a phone UA on a wide window still counts as desktop.
function isMobileDevice(): boolean {
  try {
    const ua = (navigator as unknown as { userAgent?: string }).userAgent ?? '';
    const uaMobile = /Mobi|Android|iPhone|iPad|iPod/i.test(ua);
    const mm = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia('(max-width: 768px)').matches ||
        window.matchMedia('(pointer: coarse)').matches
      : false;
    return uaMobile || mm;
  } catch {
    return false;
  }
}

export function conciergeAvailability(): { ok: boolean; reason: string } {
  // Mobile gate runs FIRST — it must override the force flag to protect the battery.
  if (isMobileDevice()) return { ok: false, reason: 'mobile device (LLM disabled to spare battery)' };
  if (FORCE_CONCIERGE) return { ok: true, reason: 'forced (VITE_FORCE_CONCIERGE=1)' };
  try {
    const nav = navigator as unknown as {
      connection?: { saveData?: boolean; effectiveType?: string };
      deviceMemory?: number;
      userAgent?: string;
    };
    const conn = nav.connection;
    if (conn?.saveData) return { ok: false, reason: 'Save-Data enabled' };
    if (conn?.effectiveType && /(^|-)(2g|3g)$/.test(conn.effectiveType))
      return { ok: false, reason: `slow connection (${conn.effectiveType})` };
    if (typeof nav.deviceMemory === 'number' && nav.deviceMemory > 0 && nav.deviceMemory < 4)
      return { ok: false, reason: `low device memory (${nav.deviceMemory}GB)` };
    return { ok: true, reason: 'ok' };
  } catch {
    return { ok: false, reason: 'navigator unavailable' };
  }
}
function deviceCanRunConcierge(): boolean {
  return conciergeAvailability().ok;
}

// Refine the raw query into an LlmIntent (a delta the caller merges over the
// rule-based interpretation). Resolves to null on an empty query, a constrained
// device, a parse miss, or any model hiccup — the caller then simply keeps the
// deterministic rule result.
export async function interpretQueryLLM(raw: string): Promise<LlmIntent | null> {
  if (!raw.trim() || !deviceCanRunConcierge()) return null;
  try {
    const text = await chat(INTERPRET_SYSTEM, raw.trim(), 96);
    const obj = extractJson(text);
    return obj ? intentFromLlmJson(obj) : null;
  } catch {
    return null;
  }
}

// ---- re-ranking: reorder the semantic candidates by TRUE fit ----
// Semantic search ranks by vibe and over-weights a salient noun: "healthy snacks"
// surfaces a "Snack Shack" that serves fried food, because the embedding matches
// "snack" and under-weights the qualifier "healthy". This is the LLM doing the one
// thing the embedder can't: reading each candidate's actual categories/description
// and judging whether it GENUINELY satisfies the request. It only reorders — every
// candidate is still returned, the bad matches just sink — so a wrong call is cheap.
const RERANK_SYSTEM =
  'You are a local discovery concierge re-ranking search results. ' +
  'Given a USER REQUEST and a numbered list of CANDIDATES (each with its categories ' +
  'and a short description), decide which candidates GENUINELY satisfy the request and ' +
  'order them best first. Judge on real fit, not name overlap: a place called ' +
  '"Snack Shack" that serves fried food does NOT satisfy "healthy snacks". ' +
  'Respond with ONLY a JSON array of the candidate numbers, best match first, e.g. ' +
  '[3,1,7]. Include every number that is a reasonable fit and omit ones that clearly ' +
  'do not fit. If none fit, output [].';

// One compact line per candidate — name, a few categories, a clipped description.
// Just enough for the model to judge fit without blowing the context on a 0.5B.
function rerankLine(e: EntitySummary): string {
  const f = e.fields as Record<string, unknown>;
  const cats = [...new Set([...parseList(f.categories), ...parseList(f.tags)])];
  const catStr = cats.length ? ` — ${cats.slice(0, 5).join(', ')}` : '';
  const desc = (f.editorialSummary as string) ?? (f.description as string) ?? (f.text as string) ?? '';
  const descStr = typeof desc === 'string' && desc.trim() ? ` — ${desc.trim().slice(0, 120)}` : '';
  return `${e.title}${catStr}${descStr}`;
}

// Pull the first JSON array of 1-based candidate indices out of the model's text.
function extractIndexArray(text: string, max: number): number[] | null {
  const start = text.indexOf('[');
  const end = text.indexOf(']', start);
  if (start === -1 || end <= start) return null;
  try {
    const arr = JSON.parse(text.slice(start, end + 1));
    if (!Array.isArray(arr)) return null;
    const nums = arr.filter(
      (n): n is number => typeof n === 'number' && Number.isInteger(n) && n >= 1 && n <= max,
    );
    return nums.length ? nums : null;
  } catch {
    return null;
  }
}

// Re-rank `candidates` by true fit and return their pointIds in the new order.
// The model's picks lead (best first); any candidate it didn't rank trails in the
// original semantic order, so nothing is ever dropped. Resolves to null on an empty
// request, a constrained device, a parse miss, or "none fit" — the caller then
// keeps the semantic order untouched.
export async function rerankByFit(
  request: string,
  candidates: EntitySummary[],
): Promise<string[] | null> {
  if (!request.trim() || candidates.length < 2) return null;
  const gate = conciergeAvailability();
  if (!gate.ok) {
    console.info('[concierge] re-rank skipped —', gate.reason);
    return null;
  }
  try {
    const list = candidates.map((e, i) => `${i + 1}. ${rerankLine(e)}`).join('\n');
    const text = await chat(RERANK_SYSTEM, `USER REQUEST: ${request}\n\nCANDIDATES:\n${list}`, 96);
    const order = extractIndexArray(text, candidates.length);
    if (!order) {
      console.info('[concierge] re-rank produced no usable order; keeping semantic order. raw:', text);
      return null;
    }
    const seen = new Set<number>();
    const ranked: string[] = [];
    for (const n of order) {
      if (!seen.has(n)) {
        seen.add(n);
        ranked.push(candidates[n - 1].pointId);
      }
    }
    // Append the candidates the model left out, keeping their semantic order.
    candidates.forEach((e, i) => {
      if (!seen.has(i + 1)) ranked.push(e.pointId);
    });
    return ranked;
  } catch {
    return null;
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
