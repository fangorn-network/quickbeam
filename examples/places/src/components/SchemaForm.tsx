// SchemaForm — a generic, schema-driven create form. Given any PublishableSchema it
// renders one labelled input per visible field, validates required fields, and calls
// the publish seam. No per-schema code: this is the reusable core of the schema
// browser's "create" surface, used both by the /create page and the inline "claim"
// flow on a Business profile.
import { useMemo, useState } from 'react';
import { useAuth } from '../lib/auth';
import { publishRecord, type PublishResult } from '../lib/publish';
import { visibleFields, type PublishableSchema } from '../lib/schemas';
import styles from './SchemaForm.module.css';

interface Props {
  schema: PublishableSchema;
  /** Pre-filled values (e.g. from the listing being claimed). */
  prefill?: Record<string, string>;
  /** Called after a successful publish/draft (e.g. to close an inline form). */
  onDone?: (result: PublishResult) => void;
  compact?: boolean;
}

export default function SchemaForm({ schema, prefill, onDone, compact }: Props) {
  const { authenticated, user, login } = useAuth();
  const fields = useMemo(() => visibleFields(schema), [schema]);

  // Seed values from prefill (only keys this schema declares) + defaults for the rest.
  const [values, setValues] = useState<Record<string, string>>(() => {
    const seed: Record<string, string> = {};
    for (const f of schema.fields) seed[f.key] = prefill?.[f.key] ?? '';
    return seed;
  });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PublishResult | null>(null);
  const [touched, setTouched] = useState(false);

  const set = (key: string, v: string) => setValues((cur) => ({ ...cur, [key]: v }));
  const missing = fields.filter((f) => f.required && !values[f.key]?.trim()).map((f) => f.key);

  const submit = async () => {
    setTouched(true);
    if (missing.length) return;
    setBusy(true);
    // The Privy embedded wallet address is the publisher/claimant identity.
    const owner = (user as { wallet?: { address?: string } } | null)?.wallet?.address ?? null;
    const r = await publishRecord({ schema, record: values, owner });
    setBusy(false);
    setResult(r);
    if (r.ok) onDone?.(r);
  };

  if (!authenticated) {
    return (
      <div className={`${styles.form} ${compact ? styles.compact : ''}`}>
        <p className={styles.gate}>
          Sign in to publish a {schema.title.toLowerCase()} — your wallet is the
          publisher identity recorded on-chain.
        </p>
        <button type="button" className={styles.primary} onClick={login}>
          Sign in to continue →
        </button>
      </div>
    );
  }

  if (result?.ok) {
    return (
      <div className={`${styles.form} ${compact ? styles.compact : ''}`}>
        <div className={`${styles.result} ${result.mode === 'onchain' ? styles.ok : styles.draft}`}>
          <strong>{result.mode === 'onchain' ? 'Published' : 'Draft saved'}</strong>
          <p>{result.message}</p>
          {result.manifestUri && <code className={styles.uri}>{result.manifestUri}</code>}
        </div>
      </div>
    );
  }

  return (
    <div className={`${styles.form} ${compact ? styles.compact : ''}`}>
      {fields.map((f) => {
        const invalid = touched && missing.includes(f.key);
        return (
          <label key={f.key} className={styles.field}>
            <span className={styles.label}>
              {f.label}
              {f.required && <span className={styles.req}> *</span>}
              {f.type === 'encrypted' && <span className={styles.lock} title="Encrypted on publish"> 🔒</span>}
            </span>
            {f.type === 'text' ? (
              <textarea
                className={`${styles.input} ${styles.textarea} ${invalid ? styles.invalid : ''}`}
                value={values[f.key] ?? ''}
                placeholder={f.placeholder}
                rows={compact ? 2 : 3}
                onChange={(e) => set(f.key, e.target.value)}
              />
            ) : (
              <input
                className={`${styles.input} ${invalid ? styles.invalid : ''}`}
                type={f.type === 'url' ? 'url' : 'text'}
                value={values[f.key] ?? ''}
                placeholder={f.placeholder}
                onChange={(e) => set(f.key, e.target.value)}
              />
            )}
            {f.help && <span className={styles.help}>{f.help}</span>}
          </label>
        );
      })}

      {result && !result.ok && <div className={styles.error}>{result.message}</div>}
      {touched && missing.length > 0 && (
        <div className={styles.error}>Fill in the required field{missing.length > 1 ? 's' : ''} above.</div>
      )}

      <button type="button" className={styles.primary} onClick={submit} disabled={busy}>
        {busy ? 'Publishing…' : `Publish ${schema.title.toLowerCase()}`}
      </button>
      <p className={styles.foot}>
        Publishes to <strong>{schema.domain}</strong> as <code>{schema.rootType}</code>. Discovery
        stays private — only this record is public.
      </p>
    </div>
  );
}
