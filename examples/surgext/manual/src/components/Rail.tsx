import { Link } from 'react-router-dom';
import TypeBadge from './TypeBadge';
import { entityHref } from '../lib/util';
import type { Point } from '../lib/types';

// A labelled row of entity chips — the "related / mesh" surface. The `mechanism`
// (e.g. "usesFilter", "semantic similarity") is shown so relationships are honest.
export default function Rail({
  title, points, total, mechanism,
}: { title: string; points: Point[]; total?: number; mechanism?: string }) {
  if (!points.length) return null;
  return (
    <div className="rail">
      <h3>
        {title}
        {total != null && <span className="count">{total}</span>}
        {mechanism && <span className="muted" style={{ textTransform: 'none', letterSpacing: 0, fontWeight: 400 }}>via {mechanism}</span>}
      </h3>
      <div className="chips">
        {points.map((p) => (
          <Link key={p.id} className="chip" to={entityHref(p.id)}>
            <TypeBadge type={p.entityType} size="sm" />
            <span className="ctitle">{String(p.fields.title ?? p.id)}</span>
            {typeof p.fields.category === 'string' && p.fields.category !== title && (
              <span className="csub">{p.fields.category}</span>
            )}
          </Link>
        ))}
      </div>
      {total != null && total > points.length && <div className="more">+{total - points.length} more</div>}
    </div>
  );
}
