import { Link } from 'react-router-dom';
import type { EntityType, PageRef } from '../lib/types';
import { useDomain } from '../lib/domainContext';
import { COPY } from '../lib/copy';
import { browseHref } from '../lib/nav';
import EntityBadge from './EntityBadge';
import styles from './LeftRail.module.css';

interface Props {
  activeType?: EntityType | null;
  counts: Partial<Record<EntityType, number | null>>;
  recent: PageRef[];
  onTypeSelect: (t: EntityType | null) => void;
  open?: boolean; // mobile drawer open
  onClose?: () => void;
}

export default function LeftRail({
  activeType,
  counts,
  recent,
  onTypeSelect,
  open,
  onClose,
}: Props) {
  const domain = useDomain();
  return (
    <>
      <div
        className={`${styles.overlay} ${open ? styles.overlayShown : ''}`}
        onClick={onClose}
        aria-hidden="true"
      />
      <nav className={`${styles.rail} ${open ? styles.railOpen : ''}`}>
      <div className={styles.section}>
        <div className={styles.sectionTitle}>Types</div>
        <ul className={styles.typeList}>
          <li>
            <Link
              to="/"
              className={`${styles.typeRow} ${!activeType ? styles.active : ''}`}
              onClick={() => onTypeSelect(null)}
            >
              <span className={styles.dot} style={{ background: 'var(--text-secondary)' }} />
              <span className={styles.typeName}>{COPY.filter.allTypes}</span>
            </Link>
          </li>
          {domain.entityTypes.map((t) => {
            const count = counts[t];
            return (
              <li key={t}>
                <Link
                  to={browseHref(t)}
                  className={`${styles.typeRow} ${activeType === t ? styles.active : ''}`}
                  onClick={() => onTypeSelect(t)}
                  title={domain.pluralOf(t)}
                >
                  <span className={styles.dot} style={{ background: domain.accentColor(t) }} />
                  <span className={styles.typeName}>{domain.pluralOf(t)}</span>
                  <span className={styles.count}>
                    {count == null ? '' : count}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      </div>

      <div className={styles.section}>
        <div className={styles.sectionTitle}>Recent</div>
        {recent.length === 0 ? (
          <div className={styles.empty}>Nothing yet</div>
        ) : (
          <ul className={styles.recentList}>
            {recent.map((p) => (
              <li key={p.href}>
                <Link to={p.href} className={styles.recentRow} title={p.label}>
                  {p.entityType && p.kind === 'entity' && (
                    <EntityBadge type={p.entityType} size="sm" />
                  )}
                  <span className={styles.recentLabel}>{p.label}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
      </nav>
    </>
  );
}
