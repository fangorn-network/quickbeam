import { useEffect, useState } from 'react';
import { kernelRecommend } from '../lib/client';
import type { Rec, Stats } from '../lib/types';
import Card from './Card';

/**
 * "Where you're heading" — the session kernel's recommendations.
 *
 * Two rails, one ranking. The main rail is the kernel's top picks, which is what a
 * real product ships. The second narrows those SAME weights to the other publisher.
 *
 * That second rail exists because the kernel is a good recommender and therefore a
 * poor demonstrator: artist affinity rightly keeps you near whoever you just played,
 * so crossing the publisher boundary doesn't show up until roughly k=40 (measured).
 * Filtering rather than re-ranking keeps it honest — if the model ranks nothing from
 * the other repo, the rail is simply absent.
 */
export default function KernelRail({
  railVersion, stats,
}: { railVersion: number; stats: Stats }) {
  const [recs, setRecs] = useState<Rec[]>([]);
  const [other, setOther] = useState<Rec[]>([]);

  // Keyed on railVersion, not version: starting a track feeds the kernel but must
  // not re-rank the grid under the click that started it. See KernelCtx.
  useEffect(() => {
    let live = true;
    void kernelRecommend(12).then((r) => { if (live) setRecs(r); });
    return () => { live = false; };
  }, [railVersion]);

  // The top picks cluster in one corner of the catalogue; this pulls the best
  // of what they left out, so the rail doesn't narrow to a single publisher.
  useEffect(() => {
    if (!recs.length) { setOther([]); return; }
    const lead = (recs[0].owner ?? '').toLowerCase();
    const opposite = stats.publishers.find((p) => p.owner.toLowerCase() !== lead);
    if (!opposite) { setOther([]); return; }
    let live = true;
    void kernelRecommend(6, opposite.owner).then((r) => { if (live) setOther(r); });
    return () => { live = false; };
  }, [recs, stats.publishers]);

  if (!recs.length) return null;

  return (
    <>
      <div className="section-head">
        <h2>Where you're heading</h2>
        <span className="count">from what you've finished and rated, computed in your browser</span>
      </div>
      <div className="grid">
        {recs.map((r) => <Card key={r.id} rec={r} queue={recs} />)}
      </div>

      {other.length > 0 && (
        <>
          <div className="section-head">
            <h2>…and further out</h2>
            <span className="count">same ranking, a different corner of the catalogue</span>
          </div>
          <div className="grid">
            {other.map((r) => <Card key={r.id} rec={r} queue={other} />)}
          </div>
        </>
      )}
    </>
  );
}
