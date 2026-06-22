import { useState } from 'react';
import type { EntityFields, TypeSchema } from '../lib/types';
import {
  FIELD_LABELS,
  SUPPRESSED_FIELDS,
  SOFT_LINK_FIELDS,
  fieldLabel,
  formatDuration,
  formatRating,
  splitList,
} from '../lib/labels';
import { fieldTypeOf } from '../lib/schemas';
import styles from './FieldTable.module.css';

interface Props {
  fields: EntityFields;
  schema: TypeSchema | null;
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

export default function FieldTable({ fields, schema, onSoftLink, onShowJson }: Props) {
  const [showAll, setShowAll] = useState(false);

  // Exclude suppressed + list fields (those go to Connections), keep scalars.
  const entries = Object.entries(fields).filter(([k, v]) => {
    if (SUPPRESSED_FIELDS.has(k)) return false;
    if (Array.isArray(v)) return false;
    if (k === 'beginYear' || k === 'endYear') return false; // merged below
    if (k === 'rating' && typeof v === 'number' && v === 0) return false;
    if (k === 'durationMs' && typeof v === 'number' && v === 0) return false;
    if (k === 'video' && v !== true) return false;
    if (k === 'cancelled' && v !== true) return false;
    if (!showAll && isEmpty(v)) return false;
    return true;
  });

  // Merge Active row.
  const activeRow = buildActiveRow(fields);

  entries.sort((a, b) => {
    const ord = (FIELD_LABELS[a[0]] ? 0 : 1) - (FIELD_LABELS[b[0]] ? 0 : 1);
    if (ord !== 0) return ord;
    return a[0].localeCompare(b[0]);
  });

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
          {activeRow && (
            <tr>
              <td className={styles.field}>Active</td>
              <td className={styles.value}>{activeRow}</td>
              <td className={styles.type}>string</td>
            </tr>
          )}
          {entries.map(([k, v]) => (
            <tr key={k} className={isEmpty(v) ? styles.emptyRow : ''}>
              <td className={styles.field}>{fieldLabel(k)}</td>
              <td className={styles.value}>
                {renderValue(k, v, onSoftLink)}
              </td>
              <td className={styles.type}>{fieldTypeOf(schema, k) ?? typeOf(v)}</td>
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

function buildActiveRow(fields: EntityFields): string | null {
  const b = fields.beginYear;
  const e = fields.endYear;
  const bs = b != null && `${b}`.length ? `${b}` : null;
  const es = e != null && `${e}`.length ? `${e}` : null;
  if (bs && es) return `${bs}–${es}`;
  if (bs && !es) return `${bs}–present`;
  if (!bs && es) return `Dissolved ${es}`;
  return null;
}

function typeOf(v: unknown): string {
  if (typeof v === 'number') return 'number';
  if (typeof v === 'boolean') return 'boolean';
  return 'string';
}

function renderValue(
  key: string,
  v: unknown,
  onSoftLink: (value: string, field: string) => void,
) {
  if (isEmpty(v)) return <span className={styles.muted}>—</span>;

  if (key === 'durationMs' && typeof v === 'number')
    return <span>{formatDuration(v)}</span>;
  if (key === 'rating' && typeof v === 'number')
    return <span>{formatRating(v)}</span>;
  if (typeof v === 'boolean') return <span>{v ? 'Yes' : 'No'}</span>;

  if (key === 'tags' && typeof v === 'string') {
    return (
      <span className={styles.pills}>
        {splitList(v).map((t) => (
          <span key={t} className={styles.pill}>
            {t}
          </span>
        ))}
      </span>
    );
  }

  if ((key === 'isrcCodes' || key === 'iswcCodes' || key === 'barcode') && typeof v === 'string') {
    return (
      <span className={styles.mono}>
        {splitList(v).join(', ') || v}
      </span>
    );
  }

  if (SOFT_LINK_FIELDS.has(key) && typeof v === 'string') {
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
