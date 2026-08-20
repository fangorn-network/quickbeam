import { Link } from 'react-router-dom';
import { entityHref, paramDesc } from '../lib/util';
import type { Point } from '../lib/types';

export default function ParamTable({ params }: { params: Point[] }) {
  if (!params.length) return null;
  return (
    <table className="params">
      <thead>
        <tr><th>Parameter</th><th>Description</th><th>Range</th></tr>
      </thead>
      <tbody>
        {params.map((p) => (
          <tr key={p.id}>
            <td><Link className="pname" to={entityHref(p.id)}>{String(p.fields.title ?? '')}</Link></td>
            <td>{paramDesc(p)}</td>
            <td className="prange">{String(p.fields.range ?? '')}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
