import { useEffect, useState } from 'react';
import { useSearchParams, Link } from 'react-router-dom';
import { useAsync } from '../lib/useAsync';
import { search, type SearchResult } from '../lib/data';
import { entityHref, snippet } from '../lib/util';
import TypeBadge from '../components/TypeBadge';
import type { Hit } from '../lib/types';

const ORDER = ['Section', 'Effect', 'OscillatorType', 'FilterType', 'ModulationSource', 'Module', 'Parameter', 'Patch'];
const GROUP_CAP = 12; // rows per group before "Show all"
const WINDOW = 80;    // top results shown before "Fetch more"
const MAX = 500;      // deepest the corpus is ranked per query (the fetch-more ceiling)

export default function SearchResults() {
  const [sp] = useSearchParams();
  const q = sp.get('q') ?? '';
  // One ranked pass per query (up to MAX). "Fetch more" just widens the client-side
  // window over this — no re-fetch, no re-embed, no results flash.
  const res = useAsync<SearchResult>(
    () => (q.trim() ? search(q, { limit: MAX }) : Promise.resolve({ hits: [], mode: 'semantic' })),
    [q],
  );
  const [visible, setVisible] = useState(WINDOW);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  useEffect(() => { setVisible(WINDOW); setExpanded(new Set()); }, [q]); // new query → back to the top window

  const toggle = (t: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(t)) n.delete(t); else n.add(t);
      return n;
    });

  if (!q.trim()) return <div className="reading"><p className="muted">Type a query to search.</p></div>;
  if (res.loading) {
    return <div className="reading"><p className="muted">Searching “{q}” — the first search downloads the embedding model…</p></div>;
  }

  const { hits, mode } = res.data ?? { hits: [], mode: 'semantic' as const };
  const windowed = hits.slice(0, visible);
  const groups: Record<string, Hit[]> = {};
  for (const h of windowed) (groups[h.entityType] ??= []).push(h);
  const types = Object.keys(groups).sort((a, b) => {
    const ia = ORDER.indexOf(a), ib = ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  const remaining = hits.length - windowed.length;

  return (
    <div className="reading">
      <h1 className="title" style={{ fontSize: '1.6rem' }}>Results for “{q}”</h1>
      <p className="muted">
        Showing top {windowed.length} match{windowed.length === 1 ? '' : 'es'} across {types.length} type{types.length === 1 ? '' : 's'}
      </p>

      {mode === 'keyword' && (
        <div className="notice">
          <b>Semantic search unavailable</b> — showing keyword matches. The in-browser embedding
          model couldn’t load (a flaky connection or an ad/script blocker can cause this), so results
          are ranked by word overlap rather than meaning. Reload to try again.
        </div>
      )}

      {hits.length === 0 && <p className="muted">No matches for “{q}”.</p>}

      {types.map((t) => {
        const all = groups[t];
        const open = expanded.has(t);
        const shown = open ? all : all.slice(0, GROUP_CAP);
        return (
          <div key={t} className="results-group">
            <h2>{t} <span className="muted">({all.length})</span></h2>
            {shown.map((h) => (
              <Link key={h.id} className="result" to={entityHref(h.id)}>
                <TypeBadge type={h.entityType} size="sm" />
                <div className="r-main">
                  <div className="r-title">{String(h.fields.title ?? h.id)}</div>
                  <div className="r-snip">{snippet(h)}</div>
                </div>
                <div className="r-score">{h.score.toFixed(3)}</div>
              </Link>
            ))}
            {all.length > GROUP_CAP && (
              <button type="button" className="showmore" onClick={() => toggle(t)}>
                {open ? 'Show fewer' : `Show all ${all.length}`}
              </button>
            )}
          </div>
        );
      })}

      {remaining > 0 && (
        <div className="fetchmore-wrap">
          <button type="button" className="fetchmore" onClick={() => setVisible((v) => Math.min(v + WINDOW, hits.length))}>
            Fetch more results
          </button>
          <span className="muted">deeper matches, lower relevance · {remaining} more</span>
        </div>
      )}
    </div>
  );
}
