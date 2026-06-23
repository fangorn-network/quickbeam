import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { PageRef } from '../lib/types';
import { useDomain } from '../lib/domainContext';
import { COPY } from '../lib/copy';
import { search, toSummary } from '../lib/qdrant';
import { browseHref, entityHref, entityPageRef } from '../lib/nav';
import EntityBadge from './EntityBadge';
import styles from './CommandPalette.module.css';

interface Props {
  open: boolean;
  onClose: () => void;
  recent: PageRef[];
  onVisit: (page: PageRef) => void;
}

interface Item {
  key: string;
  group: string;
  badge?: string;
  label: string;
  secondary?: string;
  href: string;
  page?: PageRef;
}

export default function CommandPalette({ open, onClose, recent, onVisit }: Props) {
  const navigate = useNavigate();
  const domain = useDomain();
  const [q, setQ] = useState('');
  const [results, setResults] = useState<Item[]>([]);
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQ('');
      setResults([]);
      setCursor(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  // Debounced live search.
  useEffect(() => {
    if (!open || !q.trim()) {
      setResults([]);
      return;
    }
    const handle = setTimeout(async () => {
      try {
        const { points } = await search({ q, limit: 8 });
        setResults(
          points.map((p) => {
            const s = toSummary(p);
            return {
              key: `r-${s.pointId}`,
              group: COPY.cmdk.groupResults,
              badge: s.entityType,
              label: s.title,
              secondary: domain.secondaryLine(s),
              href: entityHref(s.pointId),
              page: entityPageRef(s),
            };
          }),
        );
      } catch {
        setResults([]);
      }
    }, 200);
    return () => clearTimeout(handle);
  }, [q, open, domain]);

  const typeItems: Item[] = useMemo(() => {
    const lc = q.trim().toLowerCase();
    return domain.entityTypes.filter(
      (t) => !lc || domain.pluralOf(t).toLowerCase().includes(lc),
    ).map((t) => ({
      key: `t-${t}`,
      group: COPY.cmdk.groupTypes,
      badge: t,
      label: `Browse ${domain.pluralOf(t)}`,
      href: browseHref(t),
    }));
  }, [q, domain]);

  const recentItems: Item[] = useMemo(
    () =>
      (q.trim() ? [] : recent.slice(0, 5)).map((p) => ({
        key: `re-${p.href}`,
        group: COPY.cmdk.groupRecent,
        badge: p.kind === 'entity' ? (p.entityType as string) : undefined,
        label: p.label,
        href: p.href,
        page: p,
      })),
    [recent, q],
  );

  const items = useMemo(
    () => [...typeItems, ...recentItems, ...results],
    [typeItems, recentItems, results],
  );

  useEffect(() => {
    if (cursor >= items.length) setCursor(0);
  }, [items.length, cursor]);

  function go(item: Item | undefined) {
    if (!item) return;
    if (item.page) onVisit(item.page);
    navigate(item.href);
    onClose();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      onClose();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => Math.min(c + 1, items.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      go(items[cursor]);
    }
  }

  if (!open) return null;

  // Render grouped, but track flat index for keyboard cursor.
  let flatIndex = -1;
  const groups = [COPY.cmdk.groupTypes, COPY.cmdk.groupRecent, COPY.cmdk.groupResults];

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className={styles.input}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={COPY.cmdk.placeholder}
        />
        <div className={styles.list}>
          {items.length === 0 && (
            <div className={styles.empty}>Type to search entities or jump to a type.</div>
          )}
          {groups.map((g) => {
            const gi = items.filter((it) => it.group === g);
            if (gi.length === 0) return null;
            return (
              <div key={g} className={styles.group}>
                <div className={styles.groupTitle}>{g}</div>
                {gi.map((it) => {
                  flatIndex += 1;
                  const idx = flatIndex;
                  return (
                    <button
                      key={it.key}
                      type="button"
                      className={`${styles.row} ${cursor === idx ? styles.active : ''}`}
                      onMouseEnter={() => setCursor(idx)}
                      onClick={() => go(it)}
                    >
                      {it.badge && <EntityBadge type={it.badge} size="sm" />}
                      <span className={styles.rowLabel}>{it.label}</span>
                      {it.secondary && (
                        <span className={styles.rowSecondary}>{it.secondary}</span>
                      )}
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
        <div className={styles.footer}>↑↓ navigate · ↵ open · esc close</div>
      </div>
    </div>
  );
}
