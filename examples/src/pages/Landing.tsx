import { useNavigate } from 'react-router-dom';
import SearchBar from '../components/SearchBar';
import TypeBrowseGrid from '../components/TypeBrowseGrid';
import EntityBadge from '../components/EntityBadge';
import Breadcrumb from '../components/Breadcrumb';
import type { EntityType, PageRef } from '../lib/types';
import { COPY } from '../lib/copy';
import { browseHref, searchHref, searchPageRef } from '../lib/nav';
import { useBackStack } from '../hooks/useBackStack';
import styles from './Landing.module.css';

interface Props {
  counts: Partial<Record<EntityType, number | null>>;
  onVisit: (p: PageRef) => void;
}

export default function Landing({ counts, onVisit }: Props) {
  const navigate = useNavigate();
  const { recent } = useBackStack();

  function onSearch(q: string, type?: EntityType) {
    if (!q) return;
    onVisit(searchPageRef(q, type));
    navigate(searchHref(q, type));
  }

  return (
    <div className={styles.page}>
      <Breadcrumb crumbs={[{ label: 'Browse' }]} />

      <div className={styles.searchArea}>
        <SearchBar onSearch={onSearch} />
      </div>

      <h2 className={styles.h2}>{COPY.browse.heading}</h2>
      <TypeBrowseGrid
        typeCounts={counts}
        onTypeSelect={(t) => navigate(browseHref(t))}
      />

      <h2 className={styles.h2}>{COPY.browse.recentHeading}</h2>
      {recent.length === 0 ? (
        <div className={styles.empty}>{COPY.browse.recentEmpty}</div>
      ) : (
        <ul className={styles.recent}>
          {recent.map((p) => (
            <li key={p.href}>
              <button
                type="button"
                className={styles.recentRow}
                onClick={() => navigate(p.href)}
              >
                {p.entityType && p.kind === 'entity' && (
                  <EntityBadge type={p.entityType} size="sm" />
                )}
                <span className={styles.recentLabel}>{p.label}</span>
                <span className={styles.recentKind}>{p.kind}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
