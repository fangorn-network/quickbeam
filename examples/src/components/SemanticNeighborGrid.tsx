import EntityBadge from './EntityBadge';
import SkeletonBlock from './SkeletonBlock';
import StatusBadge from './StatusBadge';
import type { EntitySummary } from '../lib/types';
import { secondaryLine } from '../lib/summary';
import { COPY } from '../lib/copy';
import styles from './SemanticNeighborGrid.module.css';

interface Props {
  title: string;
  neighbors: EntitySummary[];
  loading: boolean;
  error?: boolean;
  onItemClick: (e: EntitySummary) => void;
}

export default function SemanticNeighborGrid({
  title,
  neighbors,
  loading,
  error,
  onItemClick,
}: Props) {
  return (
    <section className={styles.section}>
      <div className={styles.head}>
        <span className={styles.heading}>{COPY.similar.heading}</span>
        <span className={styles.sub}>{COPY.similar.subheading(title)}</span>
      </div>
      {loading ? (
        <div className={styles.grid}>
          {Array.from({ length: 6 }).map((_, i) => (
            <SkeletonBlock key={i} height="3rem" />
          ))}
        </div>
      ) : error ? (
        <div className={styles.empty}>{COPY.similar.empty}</div>
      ) : neighbors.length === 0 ? (
        <div className={styles.empty}>{COPY.similar.empty}</div>
      ) : (
        <div className={styles.grid}>
          {neighbors.map((n) => (
            <button
              key={n.pointId}
              type="button"
              className={styles.card}
              onClick={() => onItemClick(n)}
            >
              <div className={styles.cardTop}>
                <EntityBadge type={n.entityType} size="sm" />
                {n.score != null && (
                  <StatusBadge
                    variant="info"
                    label={n.score.toFixed(2)}
                    title={COPY.similar.scoreTooltip}
                  />
                )}
              </div>
              <div className={styles.title}>{n.title}</div>
              <div className={styles.secondary}>{secondaryLine(n)}</div>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
