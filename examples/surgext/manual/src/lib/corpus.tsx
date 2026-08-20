// Loads the whole corpus once (points + TOC tree + type presentation) and shares it
// via context, so every page reads from memory. The heavy shard download happens here.
import { createContext, useContext, type ReactNode } from 'react';
import { useAsync } from './useAsync';
import { allPoints } from './data';
import { buildToc, flattenToc, type TocNode } from './toc';
import { typeMetas, type TypeMeta } from './domain';
import type { Point } from './types';

export interface Corpus {
  points: Point[];
  byId: Map<string, Point>;
  toc: TocNode[];
  flat: TocNode[];
  metas: Record<string, TypeMeta>;
  counts: Record<string, number>;
}

const Ctx = createContext<Corpus | null>(null);
export function useCorpus(): Corpus {
  const c = useContext(Ctx);
  if (!c) throw new Error('useCorpus outside CorpusProvider');
  return c;
}

export function CorpusProvider({ children }: { children: ReactNode }) {
  const st = useAsync<Corpus>(async () => {
    const points = await allPoints();
    const metas = await typeMetas();
    const toc = buildToc(points);
    const counts: Record<string, number> = {};
    for (const p of points) counts[p.entityType] = (counts[p.entityType] ?? 0) + 1;
    return { points, byId: new Map(points.map((p) => [p.id, p])), toc, flat: flattenToc(toc), metas, counts };
  }, []);

  if (st.loading) return <div className="loading">Loading the manual…</div>;
  if (st.error || !st.data) {
    return <div className="error">Couldn’t load the manual data.<br /><span className="muted">{String(st.error)}</span></div>;
  }
  return <Ctx.Provider value={st.data}>{children}</Ctx.Provider>;
}
