import { useEffect, useState } from 'react';
import { kernelRecommend } from '../lib/client';
import { shortAddr } from '../lib/format';
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
  version, stats,
}: { version: number; stats: Stats }) {
  const [recs, setRecs] = useState<Rec[]>([]);
  const [other, setOther] = useState<Rec[]>([]);
  const [otherOwner, setOtherOwner] = useState<string>('');

  useEffect(() => {
    let live = true;
    void kernelRecommend(12).then((r) => { if (live) setRecs(r); });
    return () => { live = false; };
  }, [version]);

  // Whichever publisher the top picks came from, show the other one's best.
  useEffect(() => {
    if (!recs.length) { setOther([]); return; }
    const lead = (recs[0].owner ?? '').toLowerCase();
    const opposite = stats.publishers.find((p) => p.owner.toLowerCase() !== lead);
    if (!opposite) { setOther([]); return; }
    let live = true;
    setOtherOwner(opposite.owner);
    void kernelRecommend(6, opposite.owner).then((r) => { if (live) setOther(r); });
    return () => { live = false; };
  }, [recs, stats.publishers]);

  if (!recs.length) return null;

  return (
    <>
      <div className="section-head">
        <h2>Where you're heading</h2>
        <span className="count">from what you've played, computed in your browser</span>
      </div>
      <div className="grid">
        {recs.map((r) => <Card key={r.id} rec={r} queue={recs} />)}
      </div>

      {other.length > 0 && (
        <>
          <div className="section-head">
            <h2>…and from the other repo</h2>
            <span className="count">
              same ranking, narrowed to {shortAddr(otherOwner)}
            </span>
          </div>
          <div className="grid">
            {other.map((r) => <Card key={r.id} rec={r} queue={other} />)}
          </div>
        </>
      )}
    </>
  );
}
