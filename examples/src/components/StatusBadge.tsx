import styles from './StatusBadge.module.css';

interface Props {
  variant: 'success' | 'warning' | 'error' | 'info';
  label: string;
  title?: string;
}

export default function StatusBadge({ variant, label, title }: Props) {
  return (
    <span className={`${styles.badge} ${styles[variant]}`} title={title}>
      {label}
    </span>
  );
}
