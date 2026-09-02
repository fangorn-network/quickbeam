// Promise-RPC over the worker. The main thread's entire relationship with the data
// is this file: send an op, await a result. It never touches a vector.
import { CDN_DOMAIN, CDN_URL, PLATFORM_OWNER } from './config';
import type { KernelPersist, OnboardingOptions } from './graph';

export type { OnboardingOptions };
import type { Rec, RelationGroup, Stats } from './types';

export interface Progress { phase: string; pct: number; detail?: string }

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void };

const worker = new Worker(new URL('../worker/search.worker.ts', import.meta.url), { type: 'module' });
const pending = new Map<number, Pending>();
let nextId = 1;

let progressCb: ((p: Progress) => void) | null = null;
export function onProgress(cb: (p: Progress) => void) { progressCb = cb; }

worker.onmessage = (ev: MessageEvent) => {
  const msg = ev.data;
  if (msg.type === 'progress') { progressCb?.(msg as Progress); return; }
  const p = pending.get(msg.id);
  if (!p) return;
  pending.delete(msg.id);
  if (msg.type === 'error') p.reject(new Error(msg.error));
  else p.resolve(msg.result);
};

function call<T>(op: string, args?: Record<string, unknown>): Promise<T> {
  const id = nextId++;
  return new Promise<T>((resolve, reject) => {
    pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
    worker.postMessage({ id, op, args });
  });
}

let _ready: Promise<Stats> | null = null;
/** Idempotent: every view can await this without racing a second load. */
export function ready(): Promise<Stats> {
  return (_ready ??= call<Stats>('load', {
    cdn: CDN_URL, domain: CDN_DOMAIN, platform: PLATFORM_OWNER,
  }));
}

export const search = (q: string, k = 40, type?: string) => call<Rec[]>('search', { q, k, type });
export const entity = (id: string) => call<Rec | null>('entity', { id });
/** Resolve many ids in one round trip. The result is index-aligned with `ids`;
 *  a null means that id is not in this snapshot. */
export const entities = (ids: string[]) => call<(Rec | null)[]>('entities', { ids });
export const relations = (id: string) => call<RelationGroup[]>('relations', { id });
export const neighbours = (id: string, rel: string, dir: 'out' | 'in', limit = 12) =>
  call<{ records: Rec[]; total: number }>('neighbours', { id, rel, dir, limit });
export const sample = (type: string, limit = 12, owner?: string) =>
  call<Rec[]>('sample', { type, limit, owner });
/** Derived discovery rails for an artist page — see Graph.discovery. */
export const discovery = (id: string, k = 12) =>
  call<{ peers: Rec[]; similar: Rec[] }>('discovery', { id, k });

// ── session kernel ──────────────────────────────────────────────────────────
export type SignalKind = 'play' | 'skip' | 'like' | 'dislike';

/** What the readout renders. Mirrors the kernel's own `describe()`. */
export interface KernelSnapshot {
  speed: number;
  lookahead: number;
  spread: number;
  nSkips: number;
  skipRadius: number;
  timestep: number;
  entropy: number;
  topGenres: [string, number][];
  topMoods: [string, number][];
  topThemes: [string, number][];
  topArtists: [string, number][];
  durationPref: string | null;
  blacklisted: string[];
  muted: string[];
  nearThreshold: [string, number][];
  signals?: number;
}

export const kernelSignal = (id: string, kind: SignalKind) =>
  call<KernelSnapshot>('kernelSignal', { id, kind });
export const kernelRecommend = (k = 12, owner?: string) =>
  call<Rec[]>('kernelRecommend', { k, owner });
export const kernelSnapshot = () => call<KernelSnapshot>('kernelSnapshot');
export const kernelReset = () => call<KernelSnapshot>('kernelReset');
export const kernelExport = () => call<KernelPersist>('kernelExport');
export const kernelImport = (state: KernelPersist) =>
  call<KernelSnapshot>('kernelImport', { state });
export const onboardingOptions = () => call<OnboardingOptions>('onboardingOptions');
export const kernelSeed = (genres: string[], artists: string[]) =>
  call<KernelSnapshot>('kernelSeed', { genres, artists });
