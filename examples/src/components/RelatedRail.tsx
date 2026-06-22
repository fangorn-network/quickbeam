import EntityBadge from './EntityBadge';
import SkeletonBlock from './SkeletonBlock';
import type { EntitySummary } from '../lib/types';
import { secondaryLine } from '../lib/summary';
import styles from './RelatedRail.module.css';

interface Props {
  heading: string;
  mechanism: string; // small muted label, e.g. "via byArtist"
  items: EntitySummary[];
  loading?: boolean;
  totalCount?: number;
  onItemClick: (e: EntitySummary) => void;
  onViewAll?: () => void;
}

export default function RelatedRail({
  heading,
  mechanism,
  items,
  loading,
  totalCount,
  onItemClick,
  onViewAll,
}: Props) {
  const shown = items.slice(0, 5);
  return (
    <section className={styles.section}>
      <div className={styles.head}>
        <span className={styles.heading}>{heading}</span>
        <span className={styles.mechanism}>{mechanism}</span>
      </div>
      {loading ? (
        <div className={styles.loading}>
          <SkeletonBlock height="1.4rem" />
          <SkeletonBlock height="1.4rem" />
        </div>
      ) : shown.length === 0 ? (
        <div className={styles.empty}>No connections recorded for this entry.</div>
      ) : (
        <ul className={styles.list}>
          {shown.map((e) => (
            <li key={e.pointId}>
              <button
                type="button"
                className={styles.row}
                onClick={() => onItemClick(e)}
              >
                <EntityBadge type={e.entityType} size="sm" />
                <span className={styles.title}>{e.title}</span>
                <span className={styles.secondary}>{secondaryLine(e)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {!loading && totalCount != null && totalCount > shown.length && onViewAll && (
        <button type="button" className={styles.viewAll} onClick={onViewAll}>
          View all {totalCount} →
        </button>
      )}
    </section>
  );
}
