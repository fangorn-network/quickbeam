/// <reference lib="webworker" />
//
// A message shim, nothing more. All the logic lives in `lib/graph.ts` so it can be
// exercised under node without a browser (see scripts/check-graph.ts) — but it only
// ever RUNS here, off the main thread.
//
// That placement is the point. The previous demo front-end re-ranked results with a
// local 0.5B text-generation model on the main thread and froze the UI mid-search;
// `requestIdleCallback` doesn't rescue that, because once WASM decoding starts the
// thread is gone until it finishes. Keeping every model call and every scan over the
// 25k×256 vector block behind postMessage makes that class of bug impossible rather
// than merely fixed.
import { Graph, type SignalKind } from '../lib/graph';

interface Req { id: number; op: string; args?: Record<string, unknown>; }

const ctx = self as unknown as DedicatedWorkerGlobalScope;
const graph = new Graph();

ctx.onmessage = async (ev: MessageEvent<Req>) => {
  const { id, op, args = {} } = ev.data;
  try {
    let result: unknown;
    switch (op) {
      case 'load':
        result = await graph.load(
          args.cdn as string, args.domain as string, args.platform as string,
          (phase, pct, detail) => ctx.postMessage({ type: 'progress', phase, pct, detail }),
        );
        break;
      case 'search':
        result = await graph.search(args.q as string, (args.k as number) ?? 40,
                                    args.type as string | undefined);
        break;
      case 'entity':    result = graph.entity(args.id as string); break;
      case 'relations': result = graph.relations(args.id as string); break;
      case 'neighbours':
        result = graph.neighbours(args.id as string, args.rel as string,
                                  args.dir as 'out' | 'in', (args.limit as number) ?? 12);
        break;
      case 'sample':
        result = graph.sample(args.type as string, (args.limit as number) ?? 12,
                              args.owner as string | undefined);
        break;
      case 'stats':     result = graph.stats; break;
      // The kernel runs here too — it ranks over the same 25k×256 block, so it
      // belongs beside the vectors and off the main thread.
      case 'kernelSignal':
        result = graph.kernelSignal(args.id as string, args.kind as SignalKind);
        break;
      case 'kernelRecommend':
        result = graph.kernelRecommend((args.k as number) ?? 12, args.owner as string | undefined);
        break;
      case 'kernelSnapshot': result = graph.kernelSnapshot(); break;
      case 'kernelReset':    result = graph.kernelReset(); break;
      default: throw new Error(`unknown op ${op}`);
    }
    ctx.postMessage({ type: 'result', id, result });
  } catch (err) {
    ctx.postMessage({ type: 'error', id, error: err instanceof Error ? err.message : String(err) });
  }
};
