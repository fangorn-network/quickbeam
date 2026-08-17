import { Link } from 'react-router-dom';
import { useCorpus } from '../lib/corpus';
import { entityHref } from '../lib/util';

const STATS = ['Effect', 'FilterType', 'OscillatorType', 'ModulationSource', 'Parameter', 'Patch'];

export default function Home() {
  const { toc, counts, metas } = useCorpus();
  return (
    <div className="reading">
      <div className="hero">
        <h1>Surge XT — User Manual</h1>
        <p>
          The complete manual as a searchable knowledge graph — {counts.Section ?? 0} sections,
          {' '}{counts.Effect ?? 0} effects, {counts.FilterType ?? 0} filter types and
          {' '}{counts.OscillatorType ?? 0} oscillators, cross-linked to {counts.Patch ?? 0} factory
          &amp; third-party patches. Search by meaning, or browse the contents.
        </p>
      </div>
      <div className="stat-row">
        {STATS.map((t) => counts[t]
          ? <span key={t} className="stat"><b>{counts[t]}</b> {metas[t]?.plural ?? t}</span>
          : null)}
      </div>
      <div className="section-block">
        <h2>Contents</h2>
        <div className="chapters">
          {toc.map((c) => (
            <Link key={c.id} className="chapter" to={entityHref(c.id)}>
              <div className="cn">{c.title}</div>
              <div className="cc">{c.children.length} section{c.children.length === 1 ? '' : 's'}</div>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}
