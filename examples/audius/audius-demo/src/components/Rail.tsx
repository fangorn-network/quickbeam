import { useEffect, useState } from 'react';
import { neighbours } from '../lib/client';
import { relLabel } from '../lib/format';
import type { Rec, RelationGroup } from '../lib/types';
import Card from './Card';

/**
 * The shared presentation: a titled grid of records.
 *
 * Split out from `Rail` so derived rails (see Entity's discovery section) render
 * identically to real relation rails without duplicating the markup — the two
 * differ in where their records come from, not in how they look.
 */
export function RecordRail({ title, count, note, recs, footer }: {
  title: string;
  count?: number;
  /** Where these records came from, when it isn't a plain graph edge. */
  note?: string;
  recs: Rec[] | null;
  footer?: React.ReactNode;
}) {
  if (recs && recs.length === 0) return null;

  return (
    <section className="rail">
      <div className="rail-head">
        <span className="rail-title">{title}</span>
        {count !== undefined && <span className="rail-count">{count}</span>}
        {note && <span className="rail-note">{note}</span>}
      </div>
      {!recs ? (
        <div className="loading" style={{ padding: '24px 0' }}><div className="spinner" /></div>
      ) : (
        <>
          <div className="grid">{recs.map((r) => <Card key={r.id} rec={r} queue={recs} />)}</div>
          {footer}
        </>
      )}
    </section>
  );
}

/**
 * One typed relation, resolved from the graph.
 *
 * `limit` exists because a page's PRIMARY relation is its reason for existing: an
 * artist page truncating a 41-track catalogue to 12 is hiding the thing you came
 * for. Entity passes a larger one for that rail and leaves the rest at 12.
 */
export default function Rail({ group, id, limit = 12, note }: {
  group: RelationGroup; id: string; limit?: number; note?: string;
}) {
  const [recs, setRecs] = useState<Rec[] | null>(null);
  const [take, setTake] = useState(limit);

  useEffect(() => {
    let live = true;
    neighbours(id, group.rel, group.dir, take)
      .then((r) => { if (live) setRecs(r.records); })
      .catch(() => { if (live) setRecs([]); });
    return () => { live = false; };
  }, [id, group.rel, group.dir, take]);

  // Keyed on what was REQUESTED, not on what came back. Some neighbours resolve
  // to nothing, so comparing against recs.length would leave a button that
  // re-fetches the same set forever.
  const more = take < group.count;

  return (
    <RecordRail
      title={relLabel(group.rel, group.dir)}
      count={group.count}
      note={note}
      recs={recs}
      footer={more ? (
        <button className="rail-more" onClick={() => setTake(group.count)}>
          Show all {group.count}
        </button>
      ) : null}
    />
  );
}
