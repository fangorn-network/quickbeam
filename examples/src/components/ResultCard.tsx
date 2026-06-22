import EntityBadge from './EntityBadge';
import StatusBadge from './StatusBadge';
import { secondaryLine } from '../lib/summary';
import { splitList } from '../lib/labels';
import type { EntitySummary } from '../lib/types';
import styles from './ResultCard.module.css';

interface Props {
  entity: EntitySummary;
  score?: number;
  onClick: () => void;
  highlighted?: boolean;
}

export default function ResultCard({ entity, score, onClick, highlighted }: Props) {
  const secondary = secondaryLine(entity);
  const tags =
    typeof entity.fields.tags === 'string'
      ? splitList(entity.fields.tags).slice(0, 4)
      : [];
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
