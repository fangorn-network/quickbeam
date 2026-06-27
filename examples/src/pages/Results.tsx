import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import ResultCard from '../components/ResultCard';
import SkeletonBlock from '../components/SkeletonBlock';
import Icon from '../components/Icon';
import type { EntitySummary, EntityType, PageRef } from '../lib/types';
import { useDomain } from '../lib/domainContext';
import { COPY, VIBES } from '../lib/i18n';
import { scroll, search, searchNear, toSummary, QdrantError } from '../lib/qdrant';
import type { StructuredFilters } from '../lib/qdrant';
import { IS_MOCK } from '../lib/config';
import { COMMUNITY } from '../lib/community';
import { entityHref, entityPageRef, browseHref } from '../lib/nav';
import styles from './Results.module.css';

interface Facets {
  hasRating: boolean;
  hasEvents: boolean;
}

// Discover whether the collection carries ratings / events so the control tiles
// only offer toggles the data can answer (no hardcoded assumptions).
async function loadFacets(): Promise<Facets> {
  const { points } = await scroll({ limit: 200 });
  let hasRating = false;
  let hasEvents = false;
  for (const p of points) {
    const f = p.payload?.fields ?? {};
    if (typeof f.rating === 'number') hasRating = true;
    if (p.payload?.entityType === 'Event') hasEvents = true;
  }
  return { hasRating, hasEvents };
}

// Curated "vibe" pills come from the active locale profile (lib/i18n): each folds a
// short phrase into the semantic query, so a tap means "more like this feeling"
// rather than a rigid filter, and the set stays culturally grounded per community.

// Order event summaries: upcoming first (soonest), then past (most recent first).
function sortEventSummaries(items: EntitySummary[]): EntitySummary[] {
  const key = (e: EntitySummary): [number, string] => {
    const f = e.fields as Record<string, unknown>;
    return [f.isPast === true ? 1 : 0, typeof f.startDate === 'string' ? f.startDate : ''];
  };
  return [...items].sort((a, b) => {
    const [pa, da] = key(a);
    const [pb, db] = key(b);
    if (pa !== pb) return pa - pb;
    return pa === 1 ? db.localeCompare(da) : da.localeCompare(db);
  });
}

interface Props {
  onVisit: (p: PageRef) => void;
  browseType?: string; // when rendered from /browse/:entityType
}

const PAGE_SIZE = 20;

export default function Results({ onVisit, browseType }: Props) {
  const navigate = useNavigate();
  const domain = useDomain();
  const [params] = useSearchParams();
  const q = params.get('q') ?? '';
  const near = params.get('near') ?? ''; // "lat,lng" → coordinate-proximity search
  const typeParam = browseType ?? params.get('type') ?? '';
  const activeType: EntityType | '' = domain.hasType(typeParam) ? typeParam : '';
  const isBrowse = !!browseType;

  const [draft, setDraft] = useState(q);
  const [items, setItems] = useState<EntitySummary[]>([]);
  const [offset, setOffset] = useState<string | number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<'network' | 'other' | null>(null);

  const resultsRef = useRef<HTMLDivElement>(null);

  const [facets, setFacets] = useState<Facets | null>(null);
  const [vibes, setVibes] = useState<string[]>([]);
  const [upcomingOnly, setUpcomingOnly] = useState(false);
  const [dateWindow, setDateWindow] = useState<'today' | 'weekend' | 'week' | ''>('');

  // Vibe pills augment the semantic query; What's On toggles ride the structured
  // filter pipeline that search()/scroll() already understand.
  const vibeQuery = VIBES.filter((v) => vibes.includes(v.key)).map((v) => v.q).join(' ');
  const effectiveQuery = [q, vibeQuery].filter(Boolean).join(' ').trim();
  const filtersKey = `${vibeQuery}|${upcomingOnly}|${dateWindow}`;
  const hasFilters = vibes.length > 0 || upcomingOnly || !!dateWindow;

  useEffect(() => setDraft(q), [q]);

  useEffect(() => {
    if (IS_MOCK) return; // mock data has no structured fields to facet on
    loadFacets().then(setFacets).catch(() => setFacets(null));
  }, []);

  const load = useCallback(
    async (reset: boolean) => {
      if (reset) {
        setLoading(true);
        setItems([]);
        setOffset(null);
      } else {
        setLoadingMore(true);
      }
      setError(null);
      try {
        const off = reset ? null : offset;
        const filters: StructuredFilters = {
          upcomingOnly: upcomingOnly || undefined,
          dateWindow: dateWindow || undefined,
        };
        let res;
        if (near) {
          res = await searchNear({ coords: near, type: activeType || undefined, filters, limit: PAGE_SIZE, offset: off });
        } else if (effectiveQuery) {
          res = await search({ q: effectiveQuery, type: activeType || undefined, filters, limit: PAGE_SIZE, offset: off });
        } else {
          res = await scroll({ limit: PAGE_SIZE, type: activeType || undefined, filters, offset: off });
        }
        const summaries = res.points.map(toSummary);
        setItems((prev) => {
          const merged = reset ? summaries : [...prev, ...summaries];
          // With no query to rank by, order events by time — "what's on now?" first.
          return !effectiveQuery && activeType === 'Event' ? sortEventSummaries(merged) : merged;
        });
        setOffset(res.nextOffset);
      } catch (e) {
        setError(e instanceof QdrantError && e.kind === 'network' ? 'network' : 'other');
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [q, near, activeType, isBrowse, filtersKey],
  );

  useEffect(() => {
    load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, near, activeType, isBrowse, filtersKey]);

  // On mobile the controls/hero stack above the results, so a fresh search can
  // resolve entirely below the fold — it reads as "nothing happened." Once a search
  // settles, pull the results region into view. Narrow viewports only (the bento is
  // side-by-side above 900px), and only for an actual search/filter — not the
  // default landing list or "show more" pagination (those don't re-key this effect).
  useEffect(() => {
    if (loading || items.length === 0) return;
    if (!effectiveQuery && !near && !hasFilters) return;
    if (typeof window === 'undefined' || !window.matchMedia('(max-width: 900px)').matches) return;
    resultsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, q, near, filtersKey]);

  // Landing on the Events browse defaults to upcoming — "what's on now?" first.
  useEffect(() => {
    if (isBrowse && activeType === 'Event') setUpcomingOnly(true);
  }, [isBrowse, activeType]);

  function runSearch(text: string) {
    const sp = new URLSearchParams();
    if (text) sp.set('q', text);
    if (activeType) sp.set('type', activeType);
    navigate(`/search?${sp.toString()}`);
  }

  function switchType(t: EntityType | '') {
    if (isBrowse) {
      navigate(t ? browseHref(t) : '/');
    } else {
      const sp = new URLSearchParams();
      if (q) sp.set('q', q);
      if (t) sp.set('type', t);
      navigate(`/search?${sp.toString()}`);
    }
  }

  function toggleVibe(key: string) {
    setVibes((cur) => (cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key]));
  }

  function clearAll() {
    setVibes([]);
    setUpcomingOnly(false);
    setDateWindow('');
  }

  function openEntity(e: EntitySummary) {
    onVisit(entityPageRef(e));
    navigate(entityHref(e.pointId));
  }

  const headline = near
    ? COPY.results.headlineNear
    : effectiveQuery
      ? q
        ? COPY.results.headlineQuery(q)
        : COPY.results.headlineVibe
      : activeType
        ? domain.pluralOf(activeType)
        : COPY.results.headlineAround(COMMUNITY.name);

  const showEvents = facets?.hasEvents ?? true;

  return (
    <div className={styles.page}>
      {/* ---- Hero search ---- */}
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
          <button type="submit" className={styles.searchGo}>
            {COPY.search.submit}
          </button>
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

      {/* ---- Bento: control tiles + results ---- */}
      <div className={styles.bento}>
        <aside className={styles.controls}>
          <section className={`${styles.tile} ${styles.vibeTile}`}>
            <header className={styles.tileHead}>
              <Icon name="sparkle" size={15} />
              <h2 className={styles.tileTitle}>{COPY.results.vibeFinderTitle}</h2>
            </header>
            <p className={styles.tileHint}>{COPY.results.vibeFinderHint}</p>
            <div className={styles.vibeGrid}>
              {VIBES.map((v) => (
                <button
                  key={v.key}
                  type="button"
                  className={`${styles.vibe} ${vibes.includes(v.key) ? styles.vibeOn : ''}`}
                  onClick={() => toggleVibe(v.key)}
                  aria-pressed={vibes.includes(v.key)}
                >
                  <Icon name={v.icon} size={14} />
                  {v.label}
                </button>
              ))}
            </div>
          </section>

          {showEvents && (
            <section className={`${styles.tile} ${styles.whatsOnTile}`}>
              <header className={styles.tileHead}>
                <Icon name="calendar" size={15} />
                <h2 className={styles.tileTitle}>{COPY.results.whatsOnTitle}</h2>
              </header>
              <div className={styles.whatsOn}>
                <button
                  type="button"
                  className={`${styles.timeBtn} ${dateWindow === 'today' ? styles.timeOn : ''}`}
                  onClick={() => setDateWindow((c) => (c === 'today' ? '' : 'today'))}
                >
                  <span>{COPY.results.quickTonight}</span>
                  <Icon name="moon" size={15} />
                </button>
                <button
                  type="button"
                  className={`${styles.timeBtn} ${dateWindow === 'weekend' ? styles.timeOn : ''}`}
                  onClick={() => setDateWindow((c) => (c === 'weekend' ? '' : 'weekend'))}
                >
                  <span>{COPY.results.quickWeekend}</span>
                  <Icon name="sparkle" size={15} />
                </button>
                <button
                  type="button"
                  className={`${styles.timeBtn} ${activeType === 'Event' ? styles.timeOn : ''}`}
                  onClick={() => {
                    setUpcomingOnly(true);
                    switchType('Event' as EntityType);
                  }}
                >
                  <span>{COPY.results.quickEvents}</span>
                  <Icon name="arrow" size={15} />
                </button>
              </div>
            </section>
          )}

          <section className={`${styles.tile} ${styles.mapTile}`} aria-label={COPY.results.mapPreviewAria}>
            <svg className={styles.mapContour} viewBox="0 0 200 200" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
              {[20, 44, 70, 98, 128].map((r, i) => (
                <circle key={i} cx="118" cy="86" r={r} fill="none" stroke="currentColor" strokeWidth="1.4" opacity={0.5 - i * 0.06} />
              ))}
            </svg>
            <div className={styles.mapPin}>
              <Icon name="pin" size={22} />
            </div>
            <div className={styles.mapText}>
              <strong>{COPY.results.mapTeaserTitle}</strong>
              <span>{COPY.results.mapTeaserSub}</span>
            </div>
          </section>

          {hasFilters && (
            <button type="button" className={styles.clearAll} onClick={clearAll}>
              {COPY.results.resetFilters}
            </button>
          )}
        </aside>

        <div className={styles.results} ref={resultsRef}>
          <div className={styles.resultsHead}>
            <h1 className={styles.headline}>{headline}</h1>
            {!loading && !error && (
              <span className={styles.count}>
                {COPY.results.countSpots(items.length, !!offset)}
              </span>
            )}
          </div>

          {loading ? (
            <div className={styles.grid}>
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className={`${styles.skel} ${i === 0 ? styles.featured : ''}`}>
                  <SkeletonBlock height="100%" />
                </div>
              ))}
            </div>
          ) : error ? (
            <div className={styles.message}>
              {error === 'network' ? COPY.states.connectionError : COPY.states.errorNetwork}
            </div>
          ) : items.length === 0 ? (
            <div className={styles.message}>
              {effectiveQuery ? COPY.states.noResults(q || COPY.results.fallbackQuery) : COPY.results.emptyVibe}
            </div>
          ) : (
            <>
              <div className={styles.grid}>
                {items.map((e, i) => (
                  <div key={e.pointId} className={i === 0 && !!effectiveQuery ? styles.featured : undefined}>
                    <ResultCard
                      entity={e}
                      score={e.score}
                      featured={i === 0 && !!effectiveQuery}
                      onClick={() => openEntity(e)}
                    />
                  </div>
                ))}
              </div>
              {offset != null && (
                <button type="button" className={styles.more} onClick={() => load(false)} disabled={loadingMore}>
                  {loadingMore ? COPY.states.loadingEntity : COPY.results.showMore}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
