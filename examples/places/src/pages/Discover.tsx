// Discover — the unified discovery surface. One search bar, one query state (in
// the URL), one retrieval into a shared pool, and three *lenses* over that same
// result set: List (card grid), Map (geographic pins), Answer (LLM concierge).
// This replaces the old separate /search, /map and /ask pages: type once, then
// flip lenses instantly with no re-search. It is also the single place to hook
// monetization (promoted/anchor placement happens in one retrieval, see load()).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import Icon from '../components/Icon';
import type { IconName } from '../components/Icon';
import type { EntitySummary, EntityType, PageRef } from '../lib/types';
import { useDomain } from '../lib/domainContext';
import { COPY, VIBES } from '../lib/i18n';
import { scroll, search, searchNear, toSummary, QdrantError } from '../lib/qdrant';
import type { StructuredFilters } from '../lib/qdrant';
import { interpretQuery, mergeFilters, mergeInterpretations, type LlmIntent } from '../lib/queryParse';
import { interpretQueryLLM, rerankByFit, conciergeAvailability } from '../lib/llm';
import { ratingSignalOf } from '../lib/gem';
import { entityHref, entityPageRef, browseHref } from '../lib/nav';
import { COMMUNITY } from '../lib/community';
import HeadingRail from '../components/HeadingRail';
import ListLens from './lenses/ListLens';
import MapLens from './lenses/MapLens';
import styles from './Discover.module.css';

// One generous pool feeds every lens (List paginates it, Map plots the coordinate
// subset, Answer narrates the top of it). Big enough that the map reads as a real
// place; the embedder ranks it all in one pass.
const POOL = 300;

type Lens = 'list' | 'map';
const LENSES: { key: Lens; label: string; icon: IconName }[] = [
  { key: 'list', label: 'List', icon: 'sparkle' },
  { key: 'map', label: 'Map', icon: 'pin' },
];

interface Props {
  onVisit: (p: PageRef) => void;
  browseType?: string; // from /browse/:type — pins the type, no query
}

export default function Discover({ onVisit, browseType }: Props) {
  const navigate = useNavigate();
  const domain = useDomain();
  const [params, setParams] = useSearchParams();

  const q = params.get('q') ?? '';
  const near = params.get('near') ?? '';
  const focus = params.get('focus') ?? '';
  const lensParam = (params.get('lens') as Lens) || (focus ? 'map' : 'list');
  const lens: Lens = LENSES.some((l) => l.key === lensParam) ? lensParam : 'list';
  const typeParam = browseType ?? params.get('type') ?? '';
  const activeType: EntityType | '' = domain.hasType(typeParam) ? typeParam : '';

  const [draft, setDraft] = useState(q);
  const [vibes, setVibes] = useState<string[]>([]);
  const [dateWindow, setDateWindow] = useState<'today' | 'weekend' | 'week' | ''>('');
  const [upcomingOnly, setUpcomingOnly] = useState(false);
  const [gemsToggle, setGemsToggle] = useState(false);

  const [items, setItems] = useState<EntitySummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<'network' | 'other' | null>(null);

  useEffect(() => setDraft(q), [q]);
  // (Scroll-to-top on navigation is handled globally in App, on pathname change.)
  // Browsing Events defaults to upcoming — "what's on now?" first.
  useEffect(() => {
    if (activeType === 'Event') setUpcomingOnly(true);
  }, [activeType]);

  // Query understanding: lift "open now / under $$ / kid-friendly / top-rated"
  // out of the typed text into structured constraints, leaving the rest as the
  // semantic intent the vector ranks on. Applies across all three lenses. This is
  // the zero-latency RULE path — it renders the first results instantly.
  const ruleInterp = useMemo(() => interpretQuery(q), [q]);

  // …then the embedded model refines it asynchronously, catching fuzzy phrasings
  // the regexes miss ("date-night spot" → upscale + cocktails) into the same
  // closed vocabulary. Deferred to idle time so it never competes with the search
  // fetch, and fired only on a real query — so passive visitors download nothing
  // and the model warms on first search. A null result keeps the pure rule path.
  const [llmIntent, setLlmIntent] = useState<LlmIntent | null>(null);
  useEffect(() => {
    setLlmIntent(null);
    if (!q.trim()) return;
    let live = true;
    const w = window as unknown as {
      requestIdleCallback?: (cb: () => void) => number;
      cancelIdleCallback?: (h: number) => void;
    };
    const run = () =>
      interpretQueryLLM(q).then((res) => {
        if (live && res) setLlmIntent(res);
      });
    // Defer to idle time so the model load never competes with the search fetch.
    const handle = w.requestIdleCallback ? w.requestIdleCallback(run) : window.setTimeout(run, 200);
    return () => {
      live = false;
      if (w.cancelIdleCallback) w.cancelIdleCallback(handle);
      else clearTimeout(handle);
    };
  }, [q]);

  const interpreted = useMemo(
    () => (llmIntent ? mergeInterpretations(ruleInterp, llmIntent) : ruleInterp),
    [ruleInterp, llmIntent],
  );
  const vibeQuery = VIBES.filter((v) => vibes.includes(v.key)).map((v) => v.q).join(' ');
  const effectiveQuery = [interpreted.semantic, vibeQuery].filter(Boolean).join(' ').trim();
  const filters: StructuredFilters = mergeFilters(
    { upcomingOnly: upcomingOnly || undefined, dateWindow: dateWindow || undefined },
    interpreted.filters,
  );
  const filtersKey = JSON.stringify(filters);

  // ONE retrieval, shared by every lens. Deliberately independent of `lens`, so
  // toggling List ⇄ Map ⇄ Answer never refetches — same set, three renderings.
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = near
        ? await searchNear({ coords: near, type: activeType || undefined, filters, limit: POOL })
        : effectiveQuery
          ? await search({ q: effectiveQuery, type: activeType || undefined, filters, limit: POOL })
          : await scroll({ limit: POOL, type: activeType || undefined, filters });
      setItems(res.points.map(toSummary));
    } catch (e) {
      setError(e instanceof QdrantError && e.kind === 'network' ? 'network' : 'other');
      setItems([]);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveQuery, activeType, near, filtersKey]);

  useEffect(() => {
    void load();
  }, [load]);

  // On mobile the hero + filters fill the viewport, so freshly-loaded results sit
  // below the fold. When a real search finishes loading, scroll the results into
  // view — once per query, so toggling vibes/gems on the same query doesn't yank
  // the page around. Desktop is left alone (results are already visible).
  const resultsRef = useRef<HTMLDivElement>(null);
  const scrolledFor = useRef<string | null>(null);
  useEffect(() => {
    if (loading) return;
    if (!q.trim()) {
      scrolledFor.current = null;
      return;
    }
    if (scrolledFor.current === q) return;
    scrolledFor.current = q;
    if (window.matchMedia('(max-width: 768px)').matches) {
      resultsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [q, loading]);

  // LLM re-rank: the embedder ranks by vibe and over-weights a salient noun, so
  // "healthy snacks" floats a fried "Snack Shack" to the top. Once results land we
  // hand the top slice to the concierge, which reads each candidate's categories /
  // description and reorders them by TRUE fit. It only reorders the pool (nothing is
  // dropped), runs at idle so it never competes with the fetch, and on any miss we
  // keep the pure semantic order. Skipped for browse/near (no qualitative query).
  const RERANK_TOP = 12;
  const [rankOrder, setRankOrder] = useState<string[] | null>(null);
  const [ranking, setRanking] = useState(false);
  useEffect(() => {
    setRankOrder(null);
    setRanking(false);
    if (!q.trim() || near || items.length < 2 || !conciergeAvailability().ok) return;
    let live = true;
    const slice = items.slice(0, RERANK_TOP);
    const w = window as unknown as {
      requestIdleCallback?: (cb: () => void) => number;
      cancelIdleCallback?: (h: number) => void;
    };
    const run = () => {
      setRanking(true);
      void rerankByFit(q, slice).then((order) => {
        if (!live) return;
        if (order) setRankOrder(order);
        setRanking(false);
      });
    };
    const handle = w.requestIdleCallback ? w.requestIdleCallback(run) : window.setTimeout(run, 250);
    return () => {
      live = false;
      if (w.cancelIdleCallback) w.cancelIdleCallback(handle);
      else clearTimeout(handle);
    };
  }, [items, q, near]);

  // Apply the re-rank order over the pool: ranked ids lead in the model's order,
  // everything it didn't touch keeps its semantic position (stable sort).
  const orderedItems = useMemo(() => {
    if (!rankOrder) return items;
    const pos = new Map(rankOrder.map((id, i) => [id, i]));
    return [...items].sort(
      (a, b) =>
        (pos.get(a.pointId) ?? Number.MAX_SAFE_INTEGER) -
        (pos.get(b.pointId) ?? Number.MAX_SAFE_INTEGER),
    );
  }, [items, rankOrder]);

  // "Hidden gems" — a real filter, set either by the toggle or by typing it
  // ("hidden gems near the lake"). Narrows the shared pool to genuinely-loved,
  // under-discovered places (lib/gem), keeping each lens's relevance order.
  const gemsOnly = gemsToggle || !!interpreted.gemsOnly;
  const displayItems = useMemo(() => {
    if (!gemsOnly) return orderedItems;
    return orderedItems.filter((e) => ratingSignalOf(e.fields as Record<string, unknown>)?.tier === 'hidden-gem');
  }, [orderedItems, gemsOnly]);

  // ---- URL / state transitions ----
  function patchParams(mut: (sp: URLSearchParams) => void) {
    const sp = new URLSearchParams(params);
    mut(sp);
    setParams(sp);
  }
  function runSearch(text: string) {
    patchParams((sp) => {
      if (text) sp.set('q', text);
      else sp.delete('q');
      sp.delete('near');
      sp.delete('focus');
    });
  }
  function switchType(t: EntityType | '') {
    if (browseType) {
      navigate(t ? browseHref(t) : '/');
      return;
    }
    patchParams((sp) => (t ? sp.set('type', t) : sp.delete('type')));
  }
  function setLens(l: Lens) {
    patchParams((sp) => sp.set('lens', l));
  }
  function toggleVibe(key: string) {
    setVibes((cur) => (cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]));
  }
  function openEntity(e: EntitySummary) {
    onVisit(entityPageRef(e));
    navigate(entityHref(e.pointId));
  }

  const headline = near
    ? COPY.results.headlineNear
    : q
      ? COPY.results.headlineQuery(q)
      : activeType
        ? domain.pluralOf(activeType)
        : COPY.results.headlineAround(COMMUNITY.name);

  return (
    <div className={styles.page}>
      {/* ---- Shared hero: search + type ---- */}
      <section className={styles.hero}>
        <div className={styles.eyebrow}>
          <Icon name="pin" size={13} /> {COMMUNITY.name}, {COMMUNITY.regionAbbr}
        </div>
        <form
          className={styles.searchForm}
          onSubmit={(e) => {
            e.preventDefault();
            runSearch(draft.trim());
          }}
        >
          <Icon name="search" size={22} className={styles.searchIcon} />
          <input
            className={styles.searchInput}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={COPY.search.placeholder}
            aria-label={COPY.search.ariaByVibe}
            autoComplete="off"
            spellCheck={false}
          />
          {draft && (
            <button type="button" className={styles.clearInput} aria-label={COPY.search.clearAria} onClick={() => setDraft('')}>
              ×
            </button>
          )}
          <button type="submit" className={styles.searchGo}>{COPY.search.submit}</button>
        </form>

        <div className={styles.segment} role="tablist" aria-label={COPY.filter.label}>
          <button
            type="button"
            role="tab"
            aria-selected={!activeType}
            className={`${styles.seg} ${!activeType ? styles.segActive : ''}`}
            onClick={() => switchType('')}
          >
            {COPY.results.everything}
          </button>
          {domain.entityTypes.map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={activeType === t}
              className={`${styles.seg} ${activeType === t ? styles.segActive : ''}`}
              onClick={() => switchType(t)}
            >
              {domain.pluralOf(t)}
            </button>
          ))}
        </div>
      </section>

      {(interpreted.notes.length > 0 || ranking || rankOrder) && (
        <div className={styles.understood} aria-label="Filters understood from your search">
          <span className={styles.understoodLead}>Understood</span>
          {interpreted.notes.map((n) => (
            <span key={n} className={styles.understoodChip}>{n}</span>
          ))}
          {ranking && <span className={styles.understoodChip}>✦ Ranking…</span>}
          {!ranking && rankOrder && <span className={styles.understoodChip}>✦ Ranked by fit</span>}
        </div>
      )}

      {/* ---- Lens switch + shared vibe filters ---- */}
      <div className={styles.controlBar}>
        <div className={styles.lensTabs} role="tablist" aria-label="View">
          {LENSES.map((l) => (
            <button
              key={l.key}
              type="button"
              role="tab"
              aria-selected={lens === l.key}
              className={`${styles.lensTab} ${lens === l.key ? styles.lensTabActive : ''}`}
              onClick={() => setLens(l.key)}
            >
              <Icon name={l.icon} size={15} />
              {l.label}
            </button>
          ))}
        </div>

        <div className={styles.vibeRow}>
          {VIBES.map((v) => (
            <button
              key={v.key}
              type="button"
              className={`${styles.vibe} ${vibes.includes(v.key) ? styles.vibeOn : ''}`}
              onClick={() => toggleVibe(v.key)}
              aria-pressed={vibes.includes(v.key)}
            >
              <Icon name={v.icon} size={13} />
              {v.label}
            </button>
          ))}
          <button
            type="button"
            className={`${styles.vibe} ${dateWindow === 'today' ? styles.vibeOn : ''}`}
            onClick={() => setDateWindow((c) => (c === 'today' ? '' : 'today'))}
            aria-pressed={dateWindow === 'today'}
          >
            <Icon name="moon" size={13} />
            {COPY.results.quickTonight}
          </button>
          <button
            type="button"
            className={`${styles.vibe} ${dateWindow === 'weekend' ? styles.vibeOn : ''}`}
            onClick={() => setDateWindow((c) => (c === 'weekend' ? '' : 'weekend'))}
            aria-pressed={dateWindow === 'weekend'}
          >
            <Icon name="sparkle" size={13} />
            {COPY.results.quickWeekend}
          </button>
          <button
            type="button"
            className={`${styles.vibe} ${gemsOnly ? styles.vibeOn : ''}`}
            onClick={() => setGemsToggle((v) => !v)}
            aria-pressed={gemsOnly}
            title="Show only genuinely-loved, under-the-radar spots"
          >
            <Icon name="star" size={13} />
            Hidden gems
          </button>
        </div>
      </div>

      {/* ---- Ambient session model: "where you're heading" (driven by likes/dislikes) ---- */}
      <HeadingRail onOpen={openEntity} />

      {/* ---- Active lens over the shared pool ---- */}
      <div className={styles.content} ref={resultsRef}>
        {lens !== 'map' && (
          <div className={styles.resultsHead}>
            <h1 className={styles.headline}>{headline}</h1>
            {!loading && !error && lens === 'list' && (
              <span className={styles.count}>{COPY.results.countSpots(displayItems.length, false)}</span>
            )}
          </div>
        )}

        {lens === 'list' && (
          <ListLens items={displayItems} loading={loading} error={error} query={q} onOpen={openEntity} />
        )}
        {lens === 'map' && (
          <MapLens items={displayItems} loading={loading} error={error} query={q} onOpen={openEntity} focus={focus} />
        )}
      </div>
    </div>
  );
}
