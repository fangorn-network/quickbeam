import { useState } from 'react';
import type { EntityFields } from '../lib/types';
import { useDomain } from '../lib/domainContext';
import type { Domain } from '../lib/domain';
import { formatDuration, formatRating, splitList } from '../lib/labels';
import styles from './FieldTable.module.css';

interface Props {
  fields: EntityFields;
  onSoftLink: (value: string, field: string) => void;
  onShowJson: () => void;
}

// Returns true if a value counts as "empty" (suppressed per spec).
function isEmpty(v: unknown): boolean {
  if (v == null) return true;
  if (typeof v === 'string') return v.trim() === '';
  if (typeof v === 'number') return false;
  if (typeof v === 'boolean') return false;
  if (Array.isArray(v)) return v.length === 0;
  return false;
}

export default function FieldTable({ fields, onSoftLink, onShowJson }: Props) {
  const domain = useDomain();
  const [showAll, setShowAll] = useState(false);

  // Keep scalar fields + tag arrays (rendered as pills). Other array fields are the
  // "Connections" and render elsewhere. Suppressed fields never show here.
  const entries = Object.entries(fields).filter(([k, v]) => {
    if (domain.isSuppressedField(k)) return false;
    if (Array.isArray(v) && !domain.isTagField(k)) return false;
    if (!showAll && isEmpty(v)) return false;
    return true;
  });

  entries.sort((a, b) => domain.fieldLabel(a[0]).localeCompare(domain.fieldLabel(b[0])));

  return (
    <div className={styles.wrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th className={styles.thField}>Field</th>
            <th className={styles.thValue}>Value</th>
            <th className={styles.thType}>Type</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k} className={isEmpty(v) ? styles.emptyRow : ''}>
              <td className={styles.field}>{domain.fieldLabel(k)}</td>
              <td className={styles.value}>
                {renderValue(k, v, domain, onSoftLink)}
              </td>
              <td className={styles.type}>{typeOf(v)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className={styles.actions}>
        <button type="button" className={styles.toggle} onClick={() => setShowAll((s) => !s)}>
          {showAll ? 'Hide empty fields' : 'Show all fields'}
        </button>
        <button type="button" className={styles.toggle} onClick={onShowJson}>
          ▼ Show raw JSON
        </button>
      </div>
    </div>
  );
}

function typeOf(v: unknown): string {
  if (Array.isArray(v)) return 'list';
  if (typeof v === 'number') return 'number';
  if (typeof v === 'boolean') return 'boolean';
  return 'string';
}

function renderValue(
  key: string,
  v: unknown,
  domain: Domain,
  onSoftLink: (value: string, field: string) => void,
) {
  if (isEmpty(v)) return <span className={styles.muted}>—</span>;

  // Tag facets → pills (array now, comma-string for legacy payloads).
  if (domain.isTagField(key)) {
    const items = Array.isArray(v)
      ? v.filter((x): x is string => typeof x === 'string')
      : typeof v === 'string'
        ? splitList(v)
        : [];
    return (
      <span className={styles.pills}>
        {items.map((t) => (
          <span key={t} className={styles.pill}>{t}</span>
        ))}
      </span>
    );
  }

  // Generic, name-shape display heuristics (not type-switched).
  if (typeof v === 'number' && /duration/i.test(key)) return <span>{formatDuration(v)}</span>;
  if (typeof v === 'number' && /rating/i.test(key)) return <span>{formatRating(v)}</span>;
  if (typeof v === 'boolean') return <span>{v ? 'Yes' : 'No'}</span>;

  if (typeof v === 'string' && /(isrc|iswc|barcode|code)/i.test(key)) {
    return <span className={styles.mono}>{splitList(v).join(', ') || v}</span>;
  }

  // Byline / location names → run a search (soft link).
  if (domain.isSoftLinkField(key) && typeof v === 'string') {
    return (
      <button
        type="button"
        className={styles.softLink}
        title={`Search for "${v}"`}
        onClick={() => onSoftLink(v, key)}
      >
        <span className={styles.searchIcon}>⌕</span>
        {v}
      </button>
    );
  }

  if (typeof v === 'number') return <span className={styles.num}>{v}</span>;
  return <span>{String(v)}</span>;
}
