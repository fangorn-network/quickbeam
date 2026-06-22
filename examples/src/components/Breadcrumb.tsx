import { Link } from 'react-router-dom';
import styles from './Breadcrumb.module.css';

export interface Crumb {
  label: string;
  href?: string;
}

interface Props {
  crumbs: Crumb[];
}

export default function Breadcrumb({ crumbs }: Props) {
  // Truncate to last 4 on narrow viewports (handled with CSS + slice here).
  const shown = crumbs.length > 4 ? [crumbs[0], ...crumbs.slice(-3)] : crumbs;
  const collapsed = crumbs.length > 4;
  return (
    <nav className={styles.bar} aria-label="Breadcrumb">
      {shown.map((c, i) => {
        const isLast = i === shown.length - 1;
        const showEllipsis = collapsed && i === 1;
        return (
          <span key={`${c.label}-${i}`} className={styles.item}>
            {i > 0 && <span className={styles.sep}>›</span>}
            {showEllipsis && <span className={styles.sep}>…</span>}
            {isLast || !c.href ? (
              <span className={styles.current}>{c.label}</span>
            ) : (
              <Link to={c.href} className={styles.link}>
                {c.label}
              </Link>
            )}
          </span>
        );
      })}
    </nav>
  );
}
