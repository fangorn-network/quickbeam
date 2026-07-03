import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from './lib/api';
import { warmEmbedder } from './lib/embed';
import { cdnInfo, cdnSearch, reloadCdn } from './lib/cdn';
import type { DatasetView, Health, PublishedBatch, RegisteredSchema, SchemaDoc, SearchHit } from './lib/types';
import { Cid, Json } from './components/bits';

type Tab = 'schemas' | 'data' | 'published' | 'search';

const DEFAULT_SCHEMA = `{
  "name": "playground.fieldnote.v1",
  "type": "Fieldnote",
  "titleField": "title",
  "fields": {
    "title": "string",
    "note": "string",
    "place": "string"
  }
}`;

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthErr, setHealthErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('schemas');
  const [schemas, setSchemas] = useState<RegisteredSchema[]>([]);

  const refreshSchemas = useCallback(() => api.listSchemas().then((r) => setSchemas(r.schemas)).catch(() => {}), []);

  useEffect(() => {
    api.health().then(setHealth).catch((e) => setHealthErr(String(e.message ?? e)));
    refreshSchemas();
  }, [refreshSchemas]);

  const configured = Boolean(health?.configured);

  return (
    <div className="wrap">
      <header className="top">
        <div>
          <h1>Fangorn Playground</h1>
          <p className="tagline">Register schemas, publish data into the real embedding pipeline, and search it.</p>
        </div>
        <ConnBadge health={health} error={healthErr} />
      </header>

      <nav className="tabs">
        {(['schemas', 'data', 'published', 'search'] as Tab[]).map((t) => (
          <button key={t} className={t === tab ? 'active' : ''} onClick={() => setTab(t)}>
            {{ schemas: 'Schemas', data: 'Register data', published: 'Published', search: 'Search' }[t]}
          </button>
        ))}
      </nav>

      {tab === 'schemas' && <SchemasTab configured={configured} schemas={schemas} onChanged={refreshSchemas} />}
      {tab === 'data' && <DataTab configured={configured} schemas={schemas} onPublished={() => setTab('published')} />}
      {tab === 'published' && <PublishedTab />}
      {tab === 'search' && <SearchTab />}

      <footer className="foot">
        <span>owner {health?.owner ? <Cid value={health.owner} /> : '—'}</span>
        <span>chain {health?.chain ?? '—'}</span>
        <span>collection {health?.collection ?? '—'}</span>
      </footer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SCHEMAS
// ---------------------------------------------------------------------------
function SchemasTab({ configured, schemas, onChanged }: { configured: boolean; schemas: RegisteredSchema[]; onChanged: () => void }) {
  const [text, setText] = useState(DEFAULT_SCHEMA);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RegisteredSchema | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [summonName, setSummonName] = useState('');

  const register = useCallback(async () => {
    setError(null);
    let doc: SchemaDoc;
    try {
      doc = JSON.parse(text);
    } catch (e) {
      return setError(`Invalid JSON: ${(e as Error).message}`);
    }
    if (!doc.name || !doc.type || !doc.fields) return setError('Schema needs name, type, and fields.');
    setBusy(true);
    try {
      const reg = await api.register(doc);
      setResult(reg);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [text, onChanged]);

  const summon = useCallback(
    async (name: string) => {
      setError(null);
      try {
        const s = await api.summon(name);
        setText(JSON.stringify({ name: s.name, type: s.known?.type ?? titleCase(shortName(s.name)), titleField: s.known?.titleField ?? Object.keys(s.fields)[0], fields: s.fields }, null, 2));
        setResult(null);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [],
  );

  return (
    <section className="panel">
      <p className="hint">
        A schema is the shape of your records. Edit the JSON and register it — the server registers the node schema and a
        one-type bundle around it, so the real <code>quickbeam watch</code> daemon can ingest what you publish.
      </p>

      <textarea className="editor" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} rows={14} />

      <div className="row">
        <button className="primary" onClick={register} disabled={!configured || busy}>
          {busy ? 'Registering…' : 'Register schema'}
        </button>
        {!configured && <span className="muted">server not configured — see the banner above</span>}
      </div>

      {error && <div className="err-box">{error}</div>}

      {result && (
        <div className="card ok-card">
          <div className="row wrap">
            <Cid label="node schema" value={result.nodeSchemaId} />
            <Cid label="bundle schema" value={result.bundleSchemaId} />
            <span className="tag">type {result.type}</span>
            <span className="tag">dataset {result.dataset}</span>
          </div>
          <div className="watch">
            <span className="muted">Run the watcher to embed everything you publish under this schema:</span>
            <pre className="cmd">{result.watchCommand}</pre>
          </div>
        </div>
      )}

      <div className="summon">
        <h3>Summon a schema from the contract</h3>
        <div className="row">
          <input placeholder="schema name or id (e.g. playground.fieldnote.v1)" value={summonName} onChange={(e) => setSummonName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && summon(summonName)} />
          <button onClick={() => summon(summonName)} disabled={!configured || !summonName.trim()}>Load</button>
        </div>
        {schemas.length > 0 && (
          <div className="chips">
            <span className="muted">registered here:</span>
            {schemas.map((s) => (
              <button key={s.name} className="chip" onClick={() => summon(s.name)}>{s.name}</button>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// REGISTER DATA
// ---------------------------------------------------------------------------
function DataTab({ configured, schemas, onPublished }: { configured: boolean; schemas: RegisteredSchema[]; onPublished: () => void }) {
  const [active, setActive] = useState<string>('');
  const schema = useMemo(() => schemas.find((s) => s.name === active) ?? schemas[0], [schemas, active]);
  const [draft, setDraft] = useState<{ [k: string]: string }>({});
  const [batch, setBatch] = useState<{ fields: { [k: string]: string } }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<PublishedBatch | null>(null);

  useEffect(() => { setDraft({}); setBatch([]); setDone(null); setError(null); }, [schema?.name]);

  if (schemas.length === 0) {
    return <section className="panel"><p className="hint">No schemas yet — register one in the <b>Schemas</b> tab first.</p></section>;
  }

  const fieldKeys = schema ? Object.keys(schema.fields) : [];
  const canStage = schema ? Boolean(draft[schema.titleField]?.trim()) : false;

  const stage = () => {
    if (!canStage) return;
    setBatch((b) => [...b, { fields: { ...draft } }]);
    setDraft({});
  };

  const publish = async () => {
    if (!schema) return;
    const records = canStage ? [...batch, { fields: { ...draft } }] : batch;
    if (records.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.publish(schema.name, records);
      setDone(res);
      setBatch([]);
      setDraft({});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <div className="row">
        <label className="muted">Kind:</label>
        <select value={schema?.name} onChange={(e) => setActive(e.target.value)}>
          {schemas.map((s) => <option key={s.name} value={s.name}>{s.type} — {s.name}</option>)}
        </select>
      </div>

      <p className="hint">Fill the fields (placeholders come from the schema). Stage several, then publish them as one batch. Publishing writes on-chain immediately; the watcher embeds them within a poll cycle.</p>

      <div className="recform">
        {fieldKeys.map((k) => (
          <label key={k} className="field">
            <span>{k}{k === schema?.titleField ? ' *' : ''} <em>{schema?.fields[k]}</em></span>
            <input placeholder={`${k}…`} value={draft[k] ?? ''} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
          </label>
        ))}
      </div>

      <div className="row">
        <button onClick={stage} disabled={!canStage}>+ Stage another</button>
        <button className="primary" onClick={publish} disabled={!configured || busy || (batch.length === 0 && !canStage)}>
          {busy ? 'Publishing…' : `Publish ${(batch.length + (canStage ? 1 : 0)) || ''} record(s)`}
        </button>
      </div>

      {batch.length > 0 && (
        <ul className="staged">
          {batch.map((r, i) => (
            <li key={i}>
              <span className="rtitle">{schema ? r.fields[schema.titleField] : ''}</span>
              <button className="x" onClick={() => setBatch((b) => b.filter((_, j) => j !== i))}>×</button>
            </li>
          ))}
        </ul>
      )}

      {error && <div className="err-box">{error}</div>}
      {done && (
        <div className="card ok-card">
          <div className="row wrap">
            <span>Committed {done.count} record(s)</span>
            <Cid label="commit" value={done.commitCid} />
            <span className="tag">{done.uploadedCount} uploaded · {done.reusedCount} reused</span>
          </div>
          <div className="row wrap">
            <Cid label="tx" value={done.txHash} />
            <Cid label="tip" value={done.tip} />
            <button className="link" onClick={onPublished}>view published →</button>
          </div>
          <span className="muted">The watcher embeds it into Qdrant on its next poll; then it's searchable.</span>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// PUBLISHED (json viewer)
// ---------------------------------------------------------------------------
function PublishedTab() {
  const [data, setData] = useState<{ datasets: DatasetView[]; published: PublishedBatch[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => api.published().then(setData).catch((e) => setError(String(e.message ?? e))), []);
  useEffect(() => { load(); }, [load]);

  const datasets = data?.datasets ?? [];
  const commits = data?.published ?? [];

  return (
    <section className="panel">
      <div className="row">
        <button onClick={load}>Refresh</button>
        <span className="muted">{data ? `${datasets.length} dataset(s) · ${commits.length} commit(s)` : ''}</span>
      </div>
      {error && <div className="err-box">{error}</div>}
      {data && datasets.length === 0 && <p className="hint">Nothing published yet.</p>}

      {datasets.map((d, i) => (
        <details key={d.dataset} className="pub" open={i === 0}>
          <summary>
            <span className="tag">{d.type}</span>
            <span className="rtitle">{d.count} record(s) · current snapshot</span>
            <Cid label="tip" value={d.head} />
          </summary>
          <Json value={d.records} />
        </details>
      ))}

      {commits.length > 0 && (
        <>
          <h3 className="loghead">Commit history</h3>
          <ol className="clog">
            {commits.map((c, i) => (
              <li key={i}>
                <Cid value={c.commitCid} />
                <span className="rtitle">+{c.count} → {c.entryCount} total</span>
                <span className="muted">{c.uploadedCount}↑ {c.reusedCount}♻</span>
                <span className="muted">{new Date(c.at).toLocaleString()}</span>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// SEARCH
// ---------------------------------------------------------------------------
function SearchTab() {
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<{ domain: string; points: number; dim: number } | null | 'loading'>('loading');

  const loadInfo = useCallback(() => { cdnInfo().then(setInfo).catch(() => setInfo(null)); }, []);
  useEffect(() => { warmEmbedder().catch(() => {}); loadInfo(); }, [loadInfo]);

  const reload = () => { reloadCdn(); setInfo('loading'); setHits(null); loadInfo(); };

  const run = async () => {
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setHits(await cdnSearch(q.trim()));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel">
      <p className="hint">
        Semantic search over the <b>semantic CDN</b> snapshot (<code>quickbeam cdn serve</code>) — the browser downloads the
        served shards and ranks them by cosine. Document vectors are precomputed by the pipeline; only the query is embedded here.
      </p>
      <div className="row">
        {info === 'loading' && <span className="muted">loading CDN snapshot…</span>}
        {info === null && <span className="muted err-text">CDN not reachable at /cdn — run `quickbeam cdn bake` then `cdn serve --port 8090 --cors`.</span>}
        {info && info !== 'loading' && <span className="muted">domain <b>{info.domain}</b> · {info.points} points · {info.dim}-d</span>}
        <button onClick={reload}>Reload snapshot</button>
      </div>
      <div className="row search">
        <input value={q} placeholder="e.g. quiet place to launch a kayak" onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && run()} />
        <button className="primary" onClick={run} disabled={busy}>{busy ? 'Searching…' : 'Search'}</button>
      </div>
      {error && <div className="err-box">{error}</div>}
      {hits?.map((h) => (
        <details key={h.id} className="pub">
          <summary>
            <span className="score">{h.score.toFixed(3)}</span>
            {typeof h.payload.entityType === 'string' && <span className="tag">{h.payload.entityType}</span>}
            <span className="rtitle">{hitTitle(h)}</span>
          </summary>
          <Json value={h.payload} />
        </details>
      ))}
      {hits && hits.length === 0 && !busy && <p className="hint">No hits — has the watcher embedded anything yet?</p>}
    </section>
  );
}

// ---------------------------------------------------------------------------
function ConnBadge({ health, error }: { health: Health | null; error: string | null }) {
  let label = 'connecting…', cls = 'idle';
  if (error) { label = 'server offline'; cls = 'err'; }
  else if (health && !health.configured) { label = 'server up · no keys'; cls = 'warn'; }
  else if (health?.configured) { label = 'server ready'; cls = 'ok'; }
  return (
    <div className="conn">
      <span className={`pill ${cls}`}>{label}</span>
      {error && <span className="conn-hint">start it: <code>cd server && npm start</code></span>}
    </div>
  );
}

function hitTitle(h: SearchHit): string {
  const p = h.payload;
  for (const k of ['title', 'name', 'label']) if (typeof p[k] === 'string') return p[k] as string;
  return String(h.id);
}
const shortName = (n: string) => n.split('.').slice(-2, -1)[0] ?? n;
const titleCase = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
