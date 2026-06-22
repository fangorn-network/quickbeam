import { metaFor, accentColor } from '../lib/entityMeta';
import styles from './EntityBadge.module.css';

interface Props {
  type: string;
  size?: 'sm' | 'md' | 'lg';
}

export default function EntityBadge({ type, size = 'md' }: Props) {
  const meta = metaFor(type);
  const letter = meta?.letter ?? '?';
  const label = meta?.singular ?? type;
  return (
    <span
      className={`${styles.badge} ${styles[size]}`}
      style={{ '--accent': accentColor(type) } as React.CSSProperties}
      title={label}
    >
      <span className={styles.stripe} />
      <span className={styles.letter}>{letter}</span>
      {size !== 'sm' && <span className={styles.label}>{label}</span>}
    </span>
  );
}
