import { Link } from 'react-router-dom';
import { entityHref } from '../lib/util';
import type { TocNode } from '../lib/toc';

export default function PrevNext({ flat, id }: { flat: TocNode[]; id: string }) {
  const i = flat.findIndex((n) => n.id === id);
  if (i < 0) return null;
  const prev = flat[i - 1];
  const next = flat[i + 1];
  return (
    <div className="prevnext">
      {prev ? (
        <Link to={entityHref(prev.id)}>
          <div className="dir">← Previous</div>
          <div className="pn-title">{prev.title}</div>
        </Link>
      ) : <span />}
      {next ? (
        <Link className="next" to={entityHref(next.id)}>
          <div className="dir">Next →</div>
          <div className="pn-title">{next.title}</div>
        </Link>
      ) : <span />}
    </div>
  );
}
