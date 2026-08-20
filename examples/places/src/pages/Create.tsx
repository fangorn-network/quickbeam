// Create — the schema-browser "write" shell. Lists every PublishableSchema as a card;
// picking one renders its generic SchemaForm. This is the forward-looking surface:
// each new schema in lib/schemas.ts appears here automatically, so the app becomes a
// generic "browse schemas → publish a record" tool, not a one-off business-profile form.
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import SchemaForm from '../components/SchemaForm';
import { SCHEMAS, getSchema } from '../lib/schemas';
import styles from './Create.module.css';

export default function Create() {
  const { schemaId } = useParams();
  const navigate = useNavigate();
  const [params] = useSearchParams();

  // Deep-linked to a specific schema (e.g. from a "claim this profile" button).
  if (schemaId) {
    const schema = getSchema(schemaId);
    if (!schema) {
      return (
        <div className={styles.page}>
          <p className={styles.empty}>Unknown schema: <code>{schemaId}</code></p>
          <button type="button" className={styles.back} onClick={() => navigate('/create')}>
            ← All schemas
          </button>
        </div>
      );
    }
    // Prefill from query params (?title=…&locality=…) so a claim opens pre-populated.
    const prefill: Record<string, string> = {};
    for (const [k, v] of params.entries()) prefill[k] = v;

    return (
      <div className={styles.page}>
        <button type="button" className={styles.back} onClick={() => navigate('/create')}>
          ← All schemas
        </button>
        <header className={styles.head}>
          <span className={styles.bigIcon}>{schema.icon}</span>
          <div>
            <h1 className={styles.title}>{schema.title}</h1>
            <p className={styles.sub}>{schema.description}</p>
          </div>
        </header>
        <div className={styles.formWrap}>
          <SchemaForm schema={schema} prefill={prefill} />
        </div>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div>
          <h1 className={styles.title}>Publish</h1>
          <p className={styles.sub}>
            Pick a schema to publish a record. It’s registered on-chain and delivered to
            every client — no full re-index.
          </p>
        </div>
      </header>
      <div className={styles.grid}>
        {SCHEMAS.map((s) => (
          <button
            key={s.id}
            type="button"
            className={styles.card}
            onClick={() => navigate(`/create/${encodeURIComponent(s.id)}`)}
          >
            <span className={styles.cardIcon}>{s.icon}</span>
            <span className={styles.cardTitle}>{s.title}</span>
            <span className={styles.cardDesc}>{s.description}</span>
            <code className={styles.cardId}>{s.id}</code>
          </button>
        ))}
      </div>
    </div>
  );
}
