import { useDomain } from '../lib/domainContext';
import styles from './EntityBadge.module.css';

interface Props {
  type: string;
  size?: 'sm' | 'md' | 'lg';
}

export default function EntityBadge({ type, size = 'md' }: Props) {
  const domain = useDomain();
  const meta = domain.typeMeta(type);
  return (
    <span
      className={`${styles.badge} ${styles[size]}`}
      style={{ '--accent': meta.accent } as React.CSSProperties}
      title={meta.singular}
    >
      <span className={styles.stripe} />
      <span className={styles.letter}>{meta.letter}</span>
      {size !== 'sm' && <span className={styles.label}>{meta.singular}</span>}
    </span>
  );
}
