import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import Breadcrumb from '../components/Breadcrumb';
import ResultCard from '../components/ResultCard';
import SkeletonBlock from '../components/SkeletonBlock';
import { ENTITY_TYPES, isEntityType } from '../lib/types';
import type { EntitySummary, EntityType, PageRef } from '../lib/types';
import { ENTITY_META } from '../lib/entityMeta';
import { COPY } from '../lib/copy';
import { scroll, search, toSummary, typeClause, QdrantError } from '../lib/qdrant';
import type { Filter } from '../lib/qdrant';
import { entityHref, entityPageRef, browseHref } from '../lib/nav';
import styles from './Results.module.css';

interface Props {
  onVisit: (p: PageRef) => void;
  browseType?: string; // when rendered from /browse/:entityType
}

const PAGE_SIZE = 20;

export default function Results({ onVisit, browseType }: Props) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const q = params.get('q') ?? '';
  const typeParam = browseType ?? params.get('type') ?? '';
  const activeType: EntityType | '' = isEntityType(typeParam) ? typeParam : '';
  const isBrowse = !!browseType;

  const [items, setItems] = useState<EntitySummary[]>([]);
  const [offset, setOffset] = useState<string | number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<'network' | 'other' | null>(null);

  const load = useCallback(
    async (reset: boolean) => {
      if (reset) {
        setLoading(true);
        setItems([]);
        setOffset(null);
      } else {
        setLoadingMore(true);
      }
      setError(null);
      try {
        const off = reset ? null : offset;
        let res;
        if (isBrowse) {
          const filter: Filter | undefined = activeType
            ? { must: [typeClause(activeType)] }
            : undefined;
          res = await scroll({ limit: PAGE_SIZE, filter, offset: off });
        } else {
          res = await search({
            q,
            type: activeType || undefined,
            limit: PAGE_SIZE,
            offset: off,
          });
        }
        const summaries = res.points.map(toSummary);
        setItems((prev) => (reset ? summaries : [...prev, ...summaries]));
        setOffset(res.nextOffset);
      } catch (e) {
        setError(e instanceof QdrantError && e.kind === 'network' ? 'network' : 'other');
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [q, activeType, isBrowse],
  );

  useEffect(() => {
    load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, activeType, isBrowse]);

  function openEntity(e: EntitySummary) {
    onVisit(entityPageRef(e));
    navigate(entityHref(e.pointId));
  }

  function switchType(t: EntityType | '') {
    if (isBrowse) {
      navigate(t ? browseHref(t) : '/');
    } else {
      const sp = new URLSearchParams();
      if (q) sp.set('q', q);
      if (t) sp.set('type', t);
      navigate(`/search?${sp.toString()}`);
    }
  }

  const crumbs = isBrowse
    ? [{ label: 'Browse', href: '/' }, { label: activeType ? ENTITY_META[activeType].plural : 'All' }]
    : [{ label: 'Browse', href: '/' }, { label: `Search results for "${q}"` }];

  const headline = isBrowse
    ? `${activeType ? ENTITY_META[activeType].plural : 'All entries'}`
    : `Query: "${q}"`;

  return (
    <div className={styles.page}>
      <Breadcrumb crumbs={crumbs} />

      <div className={styles.layout}>
        <aside className={styles.rail}>
          <div className={styles.railTitle}>{COPY.filter.label}</div>
          <button
            type="button"
            className={`${styles.filter} ${!activeType ? styles.filterActive : ''}`}
            onClick={() => switchType('')}
          >
            {COPY.filter.allTypes}
          </button>
          {ENTITY_TYPES.map((t) => (
            <button
              key={t}
              type="button"
              className={`${styles.filter} ${activeType === t ? styles.filterActive : ''}`}
              onClick={() => switchType(t)}
            >
              {ENTITY_META[t].plural}
            </button>
          ))}
        </aside>

        <div className={styles.results}>
          <div className={styles.summary}>
            {headline}
            {activeType && !isBrowse && <> · Type: {ENTITY_META[activeType].plural}</>}
            {!loading && <> · {items.length}{offset ? '+' : ''} results</>}
          </div>

          {loading ? (
            <div className={styles.skeletons}>
              {Array.from({ length: 6 }).map((_, i) => (
                <SkeletonBlock key={i} height="3.5rem" />
              ))}
            </div>
          ) : error ? (
            <div className={styles.message}>
              {error === 'network'
                ? COPY.states.connectionError
                : COPY.states.errorNetwork}
            </div>
          ) : items.length === 0 ? (
            <div className={styles.message}>
              {isBrowse ? 'No entries of this type yet.' : COPY.states.noResults(q)}
            </div>
          ) : (
            <>
              {items.map((e) => (
                <ResultCard
                  key={e.pointId}
                  entity={e}
                  score={e.score}
                  onClick={() => openEntity(e)}
                />
              ))}
              {offset != null && (
                <button
                  type="button"
                  className={styles.more}
                  onClick={() => load(false)}
                  disabled={loadingMore}
                >
                  {loadingMore ? 'Loading…' : 'Load more'}
                </button>
              )}
            </>
          )}

          <div className={styles.mechanism}>
            {isBrowse
              ? 'Browsing: entityType filter via Qdrant scroll.'
              : 'Search mechanism: full-text on title / byArtist + type filter.'}
          </div>
        </div>
      </div>
    </div>
  );
}
