import { useDomain } from '../lib/domainContext';
import type { EntityType } from '../lib/types';
import styles from './TypeBrowseGrid.module.css';

interface Props {
  typeCounts: Partial<Record<EntityType, number | null>>;
  onTypeSelect: (t: EntityType) => void;
}

export default function TypeBrowseGrid({ typeCounts, onTypeSelect }: Props) {
  const domain = useDomain();
  return (
    <div className={styles.grid}>
      {domain.entityTypes.map((t) => {
        const meta = domain.typeMeta(t);
        const count = typeCounts[t];
        return (
          <button
            key={t}
            type="button"
            className={styles.tile}
            style={{ '--accent': meta.accent } as React.CSSProperties}
            onClick={() => onTypeSelect(t)}
          >
            <span className={styles.icon}>{meta.icon}</span>
            <span className={styles.name}>{meta.plural}</span>
            <span className={styles.count}>
              {count == null ? '— pts' : `${formatCount(count)} pts`}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function formatCount(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}
