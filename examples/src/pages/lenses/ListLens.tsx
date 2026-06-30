// List lens — the shared result pool as a ranked card grid. Presentational: it
// renders whatever Discover already fetched (no fetching of its own), so toggling
// to/from Map is instant and shows the exact same set. Client-side "show more"
// pages through the pool Discover holds.
import { useEffect, useState } from 'react';
import ResultCard from '../../components/ResultCard';
import SkeletonBlock from '../../components/SkeletonBlock';
import { COPY } from '../../lib/i18n';
import type { LensProps } from './types';
import styles from '../Results.module.css';

const PAGE = 18;

export default function ListLens({ items, loading, error, query, onOpen }: LensProps) {
  const [visible, setVisible] = useState(PAGE);
  // Reset paging whenever the underlying set changes.
  useEffect(() => setVisible(PAGE), [items]);

  if (loading) {
    return (
      <div className={styles.grid}>
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className={`${styles.skel} ${i === 0 ? styles.featured : ''}`}>
            <SkeletonBlock height="100%" />
          </div>
        ))}
      </div>
    );
  }
  if (error) {
    return <div className={styles.message}>{error === 'network' ? COPY.states.connectionError : COPY.states.errorNetwork}</div>;
  }
  if (!items.length) {
    return (
      <div className={styles.message}>
        {query ? COPY.states.noResults(query) : COPY.results.emptyVibe}
      </div>
    );
  }

  const shown = items.slice(0, visible);
  return (
    <>
      <div className={styles.grid}>
        {shown.map((e, i) => (
          <div key={e.pointId} className={i === 0 ? styles.featured : undefined}>
            <ResultCard entity={e} score={e.score} featured={i === 0} onClick={() => onOpen(e)} />
          </div>
        ))}
      </div>
      {visible < items.length && (
        <button type="button" className={styles.more} onClick={() => setVisible((v) => v + PAGE)}>
          {COPY.results.showMore}
        </button>
      )}
    </>
  );
}
