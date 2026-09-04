import { useEffect, useState } from 'react';
import Card from '../components/Card';
import SearchBar from '../components/SearchBar';
import { search } from '../lib/client';
import type { Rec } from '../lib/types';

const TYPES = ['All', 'Track', 'Artist', 'Playlist', 'Genre', 'Mood', 'Tag'];

export default function Search({ q }: { q: string }) {
  const [hits, setHits] = useState<Rec[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [type, setType] = useState('All');

  useEffect(() => {
    if (!q) return;
    let live = true;
    setHits(null);
    setErr(null);
    search(q, 60, type === 'All' ? undefined : type)
      .then((r) => { if (live) setHits(r); })
      .catch((e) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [q, type]);

  return (
    <>
      <div style={{ padding: '28px 0 8px' }}><SearchBar initial={q} /></div>

      <div className="suggestions" style={{ marginBottom: 8 }}>
        {TYPES.map((t) => (
          <button
            key={t}
            className="suggestion"
            onClick={() => setType(t)}
            style={t === type ? { borderColor: 'var(--secondary)', color: 'var(--text)' } : undefined}
            aria-pressed={t === type}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="section-head">
        <h2>{q}</h2>
        {hits && (
          <span className="count">{hits.length} results</span>
        )}
      </div>

      {err && (
        <div className="empty">
          <h3>Search failed</h3>
          <p>{err}</p>
        </div>
      )}

      {!hits && !err && (
        <div className="loading">
          {/* If this keeps turning, the main thread is free — which is the point of
              doing every model call in the worker. */}
          <div className="spinner" />
          <span>Embedding your query in the browser…</span>
        </div>
      )}

      {hits && hits.length === 0 && (
        <div className="empty">
          <h3>Nothing matched</h3>
          <p>Try describing a sound rather than naming one — "warm analog bassline".</p>
        </div>
      )}

      {hits && hits.length > 0 && (
        <div className="grid">{hits.map((h) => <Card key={h.id} rec={h} queue={hits} />)}</div>
      )}
    </>
  );
}
