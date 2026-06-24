import EntityBadge from './EntityBadge';
import StatusBadge from './StatusBadge';
import { useDomain } from '../lib/domainContext';
import type { EntitySummary } from '../lib/types';
import styles from './ResultCard.module.css';

interface Props {
  entity: EntitySummary;
  score?: number;
  onClick: () => void;
  highlighted?: boolean;
}

export default function ResultCard({ entity, score, onClick, highlighted }: Props) {
  const domain = useDomain();
  const secondary = domain.secondaryLine(entity);
  const tags = domain.primaryTags(entity.fields as Record<string, unknown>).slice(0, 4);
  return (
    <button
      type="button"
      className={`${styles.card} ${highlighted ? styles.highlighted : ''}`}
      onClick={onClick}
    >
      <div className={styles.left}>
        <EntityBadge type={entity.entityType} size="md" />
      </div>
      <div className={styles.body}>
        <div className={styles.title}>{entity.title}</div>
        {secondary && <div className={styles.secondary}>{secondary}</div>}
        {tags.length > 0 && (
          <div className={styles.tags}>
            Tags: {tags.join(' · ')}
          </div>
        )}
      </div>
      {score != null && (
        <div className={styles.score}>
          <StatusBadge
            variant="info"
            label={`score ${score.toFixed(2)}`}
            title="Relevance score"
          />
        </div>
      )}
    </button>
  );
}
