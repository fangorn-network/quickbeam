import { useEffect, useState } from 'react';
import { neighbours } from '../lib/client';
import { relLabel } from '../lib/format';
import type { Rec, RelationGroup } from '../lib/types';
import Card from './Card';

/**
 * One typed relation, resolved from the linkset.
 *
 * When `crosses` is set, at least one neighbour was published by a different wallet —
 * so following this rail leaves one publisher's graph and lands in another's. That
 * badge is the miniature of the hero's strand marker, and it is the single most
 * important thing on the page: it is the claim, demonstrated, on real records.
 */
export default function Rail({ group, id }: { group: RelationGroup; id: string }) {
  const [recs, setRecs] = useState<Rec[] | null>(null);

  useEffect(() => {
    let live = true;
    neighbours(id, group.rel, group.dir, 12)
      .then((r) => { if (live) setRecs(r.records); })
      .catch(() => { if (live) setRecs([]); });
    return () => { live = false; };
  }, [id, group.rel, group.dir]);

  if (recs && recs.length === 0) return null;

  return (
    <section className="rail">
      <div className="rail-head">
        <span className="rail-title">{relLabel(group.rel, group.dir)}</span>
        <span className="rail-count">{group.count}</span>
        {group.crosses && (
          <span className="rail-bridge" title="This relation joins records published by different wallets">
            crosses publishers
          </span>
        )}
      </div>
      {!recs ? (
        <div className="loading" style={{ padding: '24px 0' }}><div className="spinner" /></div>
      ) : (
        <div className="grid">{recs.map((r) => <Card key={r.id} rec={r} queue={recs} />)}</div>
      )}
    </section>
  );
}
