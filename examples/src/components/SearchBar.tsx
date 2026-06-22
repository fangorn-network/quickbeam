import { useEffect, useRef, useState } from 'react';
import { ENTITY_TYPES } from '../lib/types';
import type { EntityType } from '../lib/types';
import { ENTITY_META } from '../lib/entityMeta';
import { COPY } from '../lib/copy';
import styles from './SearchBar.module.css';

interface Props {
  initialValue?: string;
  initialType?: EntityType | '';
  onSearch: (q: string, type?: EntityType) => void;
}

export default function SearchBar({ initialValue = '', initialType = '', onSearch }: Props) {
  const [q, setQ] = useState(initialValue);
  const [type, setType] = useState<EntityType | ''>(initialType);
  const inputRef = useRef<HTMLInputElement>(null);

  // "Press / to focus"
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === '/' && document.activeElement?.tagName !== 'INPUT') {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    onSearch(q.trim(), type || undefined);
  }

  return (
    <form className={styles.wrap} onSubmit={submit}>
      <div className={styles.row}>
        <span className={styles.icon}>⌕</span>
        <input
          ref={inputRef}
          className={styles.input}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={COPY.search.placeholder}
          aria-label="Search"
        />
        {q && (
          <button
            type="button"
            className={styles.clear}
            aria-label={COPY.search.clearAria}
            onClick={() => setQ('')}
          >
            ×
          </button>
        )}
        <select
          className={styles.select}
          value={type}
          onChange={(e) => setType(e.target.value as EntityType | '')}
          aria-label={COPY.filter.label}
        >
          <option value="">{COPY.filter.allTypes}</option>
          {ENTITY_TYPES.map((t) => (
            <option key={t} value={t}>
              {ENTITY_META[t].plural}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.subtext}>
        {COPY.search.subtext} · {COPY.search.keyboardHint}
      </div>
    </form>
  );
}
