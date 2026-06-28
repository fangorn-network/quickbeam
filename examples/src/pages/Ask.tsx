// Ask — the concierge. Not a chat: you pose a real-world request ("pizza for 4
// guys") and watch the agent work in the open. It (1) PLANS — a small in-browser
// LLM restates your intent and tunes a search phrase, shown as chips; (2) SEARCHES
// — the existing semantic engine ranks the corpus against that phrase; (3)
// EXPLAINS — for the top matches the LLM writes a grounded, one-sentence "why it
// fits" from each place's real fields, injected above the Vibe match bar.
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import ResultCard from '../components/ResultCard';
import Icon from '../components/Icon';
import type { EntitySummary, PageRef } from '../lib/types';
import { search, toSummary } from '../lib/qdrant';
import { entityHref, entityPageRef } from '../lib/nav';
import { COMMUNITY } from '../lib/community';
import { warmLLM, onLlmStatus, planQuery, explainMatch, type LlmStatus, type QueryPlan } from '../lib/llm';
import styles from './Ask.module.css';

type Phase = 'idle' | 'planning' | 'searching' | 'explaining' | 'done';

// How many top results get a written explanation (the rest still show, ranked).
const EXPLAIN_TOP = 3;
const RESULT_LIMIT = 9;

const EXAMPLES = ['pizza for 4 guys', 'quiet spot to read on a rainy afternoon', 'where to watch the game with a crowd'];

// Module-level cache so a completed (or in-progress) search survives leaving the
// page — tap a result card, then come back and the full screen is still here. It
// lives as long as the SPA isn't hard-reloaded, which is exactly the window we
// want. We never restore a "busy" phase (no resuming generation), so a returning
// visitor lands on a settled result screen.
interface AskCache {
  draft: string;
  request: string;
  plan: QueryPlan | null;
  items: EntitySummary[];
  explanations: Record<string, string>;
}
let askCache: AskCache | null = null;

interface Props {
  onVisit?: (p: PageRef) => void;
}

export default function Ask({ onVisit }: Props) {
  const navigate = useNavigate();
  const [draft, setDraft] = useState(() => askCache?.draft ?? '');
  const [request, setRequest] = useState(() => askCache?.request ?? ''); // the submitted text
  const [phase, setPhase] = useState<Phase>(() => (askCache?.items.length ? 'done' : 'idle'));
  const [plan, setPlan] = useState<QueryPlan | null>(() => askCache?.plan ?? null);
  const [items, setItems] = useState<EntitySummary[]>(() => askCache?.items ?? []);
  const [explanations, setExplanations] = useState<Record<string, string>>(() => askCache?.explanations ?? {});
  const [explainingId, setExplainingId] = useState<string | null>(null);
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const runId = useRef(0); // guards against overlapping submissions

  // Start fetching the (large) concierge model the moment the page opens, and
  // mirror its download status into the loading banner.
  useEffect(() => {
    warmLLM();
    return onLlmStatus(setLlm);
  }, []);

  // Persist the session so navigating away and back restores the full screen.
  useEffect(() => {
    if (items.length || request) askCache = { draft, request, plan, items, explanations };
  }, [draft, request, plan, items, explanations]);

  const ask = useCallback(
    async (text: string) => {
      const q = text.trim();
      if (!q) return;
      const myRun = ++runId.current;
      setRequest(q);
      setError(null);
      setPlan(null);
      setItems([]);
      setExplanations({});
      setExplainingId(null);

      // 1) PLAN — restate intent + tune the search phrase.
      setPhase('planning');
      const p = await planQuery(q);
      if (myRun !== runId.current) return;
      setPlan(p);

      // 2) SEARCH — semantic ranking against the planned phrase.
      setPhase('searching');
      let results: EntitySummary[] = [];
      try {
        const res = await search({ q: p.query || q, limit: RESULT_LIMIT });
        results = res.points.map(toSummary);
      } catch (e) {
        if (myRun !== runId.current) return;
        setError(e instanceof Error ? e.message : 'Search failed');
        setPhase('done');
        return;
      }
      if (myRun !== runId.current) return;
      setItems(results);

      // 3) EXPLAIN — grounded "why it fits" for the top matches, streamed in.
      setPhase('explaining');
      for (const e of results.slice(0, EXPLAIN_TOP)) {
        if (myRun !== runId.current) return;
        setExplainingId(e.pointId);
        await explainMatch(q, e, (full) => {
          if (myRun === runId.current) setExplanations((prev) => ({ ...prev, [e.pointId]: full }));
        });
      }
      if (myRun !== runId.current) return;
      setExplainingId(null);
      setPhase('done');
    },
    [],
  );

  function openEntity(e: EntitySummary) {
    onVisit?.(entityPageRef(e));
    navigate(entityHref(e.pointId));
  }

  const busy = phase === 'planning' || phase === 'searching' || phase === 'explaining';
  const loadingModel = llm?.stage === 'loading';

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.eyebrow}>
          <Icon name="compass" size={14} /> {COMMUNITY.name} concierge
        </div>
        <h1 className={styles.title}>What are you in the mood for?</h1>
        <p className={styles.sub}>
          Tell us in plain words — a few people for pizza, a quiet afternoon, somewhere to catch the game.
          We’ll find the right spots and tell you <em>why</em> each one fits.
        </p>

        <form
          className={styles.form}
          onSubmit={(e) => {
            e.preventDefault();
            void ask(draft);
          }}
        >
          <Icon name="search" size={20} className={styles.formIcon} />
          <input
            className={styles.input}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="e.g. pizza for 4 guys"
            aria-label="Ask the concierge"
            autoComplete="off"
            spellCheck={false}
          />
          <button type="submit" className={styles.go} disabled={busy || !draft.trim()}>
            {busy ? '…' : 'Ask'}
          </button>
        </form>

        {phase === 'idle' && (
          <div className={styles.examples}>
            <span className={styles.examplesLabel}>Try</span>
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                type="button"
                className={styles.example}
                onClick={() => {
                  setDraft(ex);
                  void ask(ex);
                }}
              >
                {ex}
              </button>
            ))}
          </div>
        )}
      </header>

      {/* Model download / warmup banner. */}
      {loadingModel && (
        <div className={styles.modelBar}>
          <Icon name="sparkle" size={14} className={styles.modelIcon} />
          <div className={styles.modelText}>
            <span>{llm?.message ?? 'Loading the concierge…'}</span>
            <div className={styles.progress}>
              <span className={styles.progressFill} style={{ width: `${Math.round((llm?.progress ?? 0) * 100)}%` }} />
            </div>
          </div>
        </div>
      )}

      {/* Model failed to load: don't fail silently — say so (the snippets are off,
          but ranked results below still work). */}
      {llm?.stage === 'error' && (
        <div className={`${styles.modelBar} ${styles.modelError}`}>
          <Icon name="sparkle" size={14} className={styles.modelIcon} />
          <div className={styles.modelText}>
            <span>The concierge couldn’t start, so explanations are off. {llm.message}</span>
          </div>
        </div>
      )}

      {/* The reveal: what the concierge understood, then the ranked matches. */}
      {(plan || busy) && (
        <section className={styles.reveal}>
          <div className={styles.steps}>
            <Step done={!!plan} active={phase === 'planning'} label="Getting what you want" />
            <Step done={items.length > 0} active={phase === 'searching'} label="Finding the best spots" />
            <Step done={phase === 'done'} active={phase === 'explaining'} label="Telling you why they fit" />
          </div>

          {plan && (
            <div className={styles.plan}>
              <span className={styles.planIntent}>
                <Icon name="compass" size={14} /> {plan.intent}
              </span>
              {plan.tags.length > 0 && (
                <span className={styles.planTags}>
                  {plan.tags.map((t) => (
                    <span key={t} className={styles.planTag}>{t}</span>
                  ))}
                </span>
              )}
            </div>
          )}
        </section>
      )}

      {error && <div className={styles.message}>{error}</div>}

      {items.length > 0 && (
        <div className={styles.grid}>
          {items.map((e, i) => (
            <div key={e.pointId} className={i === 0 ? styles.featured : undefined}>
              <ResultCard
                entity={e}
                score={e.score}
                featured={i === 0}
                explanation={explanations[e.pointId]}
                explaining={explainingId === e.pointId && !explanations[e.pointId]}
                onClick={() => openEntity(e)}
              />
            </div>
          ))}
        </div>
      )}

      {phase === 'done' && items.length === 0 && !error && (
        <div className={styles.message}>No matches for “{request}”. Try describing the vibe differently.</div>
      )}
    </div>
  );
}

function Step({ label, active, done }: { label: string; active: boolean; done: boolean }) {
  return (
    <div className={`${styles.step} ${done ? styles.stepDone : active ? styles.stepActive : ''}`}>
      <span className={styles.stepDot}>{done ? '✓' : active ? <span className={styles.spinner} /> : ''}</span>
      {label}
    </div>
  );
}
