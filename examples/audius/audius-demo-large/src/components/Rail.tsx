import { useEffect, useState } from 'react';
import { neighbours } from '../lib/client';
import { relLabel } from '../lib/format';
import type { Rec, RelationGroup } from '../lib/types';
import Card from './Card';

/** Cards added per "Show more" press. Bounded so a hub node cannot lock the tab:
 *  against the full catalogue a Genre can hold six figures of inbound tracks. */
const PAGE = 48;

/**
 * The shared presentation: a titled grid of records.
 *
 * Split out from `Rail` so derived rails (see Entity's discovery section) render
 * identically to real relation rails without duplicating the markup — the two
 * differ in where their records come from, not in how they look.
 */
export function RecordRail({ title, count, crosses, note, recs, footer }: {
  title: string;
  count?: number;
  crosses?: boolean;
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
        {crosses && (
          <span className="rail-bridge" title="This relation joins records published by different wallets">
            crosses publishers
          </span>
        )}
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
 * One typed relation, resolved from the linkset.
 *
 * When `crosses` is set, at least one neighbour was published by a different wallet —
 * so following this rail leaves one publisher's graph and lands in another's. That
 * badge is the miniature of the hero's strand marker, and it is the single most
 * important thing on the page: it is the claim, demonstrated, on real records.
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
      crosses={group.crosses}
      note={note}
      recs={recs}
      footer={more ? (
        // Paginate rather than jumping to group.count. On the 25k snapshot
        // `setTake(group.count)` was harmless; against the full catalogue a Genre can
        // hold six figures of inbound tracks, and rendering that many Cards in one
        // grid locks the tab. Each press widens by a page, so the cost is bounded and
        // the user can still walk the whole rail.
        <button className="rail-more" onClick={() => setTake(Math.min(take + PAGE, group.count))}>
          Show {Math.min(PAGE, group.count - take)} more
          <span className="rail-more-of"> of {group.count.toLocaleString()}</span>
        </button>
      ) : null}
    />
  );
}
