import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import Breadcrumb from '../components/Breadcrumb';
import ResultCard from '../components/ResultCard';
import SkeletonBlock from '../components/SkeletonBlock';
import type { EntitySummary, EntityType, PageRef } from '../lib/types';
import { useDomain } from '../lib/domainContext';
import { COPY } from '../lib/copy';
import { scroll, search, searchNear, toSummary, QdrantError } from '../lib/qdrant';
import type { StructuredFilters } from '../lib/qdrant';
import { IS_MOCK } from '../lib/config';
import { entityHref, entityPageRef, browseHref } from '../lib/nav';
import styles from './Results.module.css';

interface Facets {
  prices: string[];
  categories: string[];
  amenities: string[];
  localities: string[];
  hasRating: boolean;
  hasEvents: boolean;
}

// amenities are stored as a JSON-encoded string (e.g. '["live music","dine-in"]').
function parseAmenities(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(String);
  if (typeof v === 'string' && v.trim().startsWith('[')) {
    try {
      const a = JSON.parse(v);
      return Array.isArray(a) ? a.map(String) : [];
    } catch {
      return [];
    }
  }
  return [];
}

// Discover available structured-filter values from a sample of the collection so
// the filter controls reflect real data (no hardcoded category/price lists).
async function loadFacets(): Promise<Facets> {
  const { points } = await scroll({ limit: 200 });
  const prices = new Set<string>();
  const catCount = new Map<string, number>();
  const amCount = new Map<string, number>();
  const locCount = new Map<string, number>();
  let hasRating = false;
  let hasEvents = false;
  for (const p of points) {
    const f = p.payload?.fields ?? {};
    if (typeof f.rating === 'number') hasRating = true;
    if (p.payload?.entityType === 'Event') hasEvents = true;
    if (typeof f.priceLevel === 'string' && f.priceLevel) prices.add(f.priceLevel);
    if (typeof f.locality === 'string' && f.locality) locCount.set(f.locality, (locCount.get(f.locality) ?? 0) + 1);
    const cats = f.categories;
    if (Array.isArray(cats)) {
      for (const c of cats) if (typeof c === 'string') catCount.set(c, (catCount.get(c) ?? 0) + 1);
    }
    for (const a of parseAmenities(f.amenities)) amCount.set(a, (amCount.get(a) ?? 0) + 1);
  }
  const byFreq = (m: Map<string, number>, n: number) =>
    [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([k]) => k);
  return {
    prices: [...prices].sort(),
    categories: byFreq(catCount, 16),
    amenities: byFreq(amCount, 12),
    localities: byFreq(locCount, 16),
    hasRating,
    hasEvents,
  };
}

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

  const [items, setItems] = useState<EntitySummary[]>([]);
  const [offset, setOffset] = useState<string | number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<'network' | 'other' | null>(null);

  // Structured filters layered on top of the query (payload filters in qdrant
  // mode; in-memory filtering in shards mode).
  const [facets, setFacets] = useState<Facets | null>(null);
  const [ratingGte, setRatingGte] = useState(0);
  const [priceLevels, setPriceLevels] = useState<string[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [amenities, setAmenities] = useState<string[]>([]);
  const [localities, setLocalities] = useState<string[]>([]);
  const [upcomingOnly, setUpcomingOnly] = useState(false);

  // Stable string key over the active filters so load() re-runs when they change.
  const filtersKey = `${ratingGte}|${priceLevels.join(',')}|${categories.join(',')}|${amenities.join(',')}|${localities.join(',')}|${upcomingOnly}`;
  const hasFilters =
    ratingGte > 0 ||
    priceLevels.length > 0 ||
    categories.length > 0 ||
    amenities.length > 0 ||
    localities.length > 0 ||
    upcomingOnly;

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
          ratingGte: ratingGte || undefined,
          priceLevels: priceLevels.length ? priceLevels : undefined,
          categories: categories.length ? categories : undefined,
          amenities: amenities.length ? amenities : undefined,
          localities: localities.length ? localities : undefined,
          upcomingOnly: upcomingOnly || undefined,
        };
        let res;
        if (near) {
          res = await searchNear({
            coords: near,
            type: activeType || undefined,
            filters,
            limit: PAGE_SIZE,
            offset: off,
          });
        } else if (isBrowse) {
          res = await scroll({
            limit: PAGE_SIZE,
            type: activeType || undefined,
            filters,
            offset: off,
          });
        } else {
          res = await search({
            q,
            type: activeType || undefined,
            filters,
            limit: PAGE_SIZE,
            offset: off,
          });
        }
        const summaries = res.points.map(toSummary);
        setItems((prev) => {
          const merged = reset ? summaries : [...prev, ...summaries];
          // Browsing Events has no query relevance to preserve, so order them by
          // time — soonest upcoming first, then past — to answer "what's on now?".
          return isBrowse && activeType === 'Event' ? sortEventSummaries(merged) : merged;
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

  // Landing on the Events browse defaults to upcoming — "what's on now?" first.
  useEffect(() => {
    if (isBrowse && activeType === 'Event') setUpcomingOnly(true);
  }, [isBrowse, activeType]);

  function toggle(list: string[], setList: (v: string[]) => void, value: string) {
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
  }

  function clearFilters() {
    setRatingGte(0);
    setPriceLevels([]);
    setCategories([]);
    setAmenities([]);
    setLocalities([]);
    setUpcomingOnly(false);
  }

  function openEntity(e: EntitySummary) {
    onVisit(entityPageRef(e));
    navigate(entityHref(e.pointId));
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

  const crumbs = near
    ? [{ label: 'Browse', href: '/' }, { label: `Nearby (${near})` }]
    : isBrowse
      ? [{ label: 'Browse', href: '/' }, { label: activeType ? domain.pluralOf(activeType) : 'All' }]
      : [{ label: 'Browse', href: '/' }, { label: `Search results for "${q}"` }];

  const headline = near
    ? `Nearest to ${near}`
    : isBrowse
      ? `${activeType ? domain.pluralOf(activeType) : 'All entries'}`
      : `Query: "${q}"`;

  return (
    <div className={styles.page}>
      <Breadcrumb crumbs={crumbs} />

      <div className={styles.layout}>
        <aside className={styles.rail}>
          <div className={styles.railTitle}>{COPY.filter.label}</div>
          <button
            type="button"
            className={`${styles.filter} ${!activeType ? styles.filterActive : ''}`}
            onClick={() => switchType('')}
          >
            {COPY.filter.allTypes}
          </button>
          {domain.entityTypes.map((t) => (
            <button
              key={t}
              type="button"
              className={`${styles.filter} ${activeType === t ? styles.filterActive : ''}`}
              onClick={() => switchType(t)}
            >
              {domain.pluralOf(t)}
            </button>
          ))}

          {facets?.hasEvents && (
            <div className={styles.facetGroup}>
              <button
                type="button"
                className={`${styles.chip} ${upcomingOnly ? styles.chipActive : ''}`}
                onClick={() => setUpcomingOnly((v) => !v)}
                title="Hide past events (bars are unaffected)"
              >
                🎫 Upcoming events only
              </button>
            </div>
          )}

          {facets?.hasRating && (
            <div className={styles.facetGroup}>
              <div className={styles.facetTitle}>
                <span>Min rating</span>
                <span className={styles.facetValue}>
                  {ratingGte > 0 ? `${ratingGte.toFixed(1)}+` : 'Any'}
                </span>
              </div>
              <input
                type="range"
                className={styles.slider}
                min={0}
                max={5}
                step={0.5}
                value={ratingGte}
                onChange={(e) => setRatingGte(Number(e.target.value))}
              />
            </div>
          )}

          {facets && facets.localities.length > 0 && (
            <div className={styles.facetGroup}>
              <div className={styles.facetTitle}>Location</div>
              <div className={styles.chips}>
                {facets.localities.map((l) => (
                  <button
                    key={l}
                    type="button"
                    className={`${styles.chip} ${localities.includes(l) ? styles.chipActive : ''}`}
                    onClick={() => toggle(localities, setLocalities, l)}
                  >
                    {l.replace(/, WI$/, '')}
                  </button>
                ))}
              </div>
            </div>
          )}

          {facets && facets.amenities.length > 0 && (
            <div className={styles.facetGroup}>
              <div className={styles.facetTitle}>Amenities</div>
              <div className={styles.chips}>
                {facets.amenities.map((a) => (
                  <button
                    key={a}
                    type="button"
                    className={`${styles.chip} ${amenities.includes(a) ? styles.chipActive : ''}`}
                    onClick={() => toggle(amenities, setAmenities, a)}
                  >
                    {a}
                  </button>
                ))}
              </div>
            </div>
          )}

          {facets && facets.prices.length > 0 && (
            <div className={styles.facetGroup}>
              <div className={styles.facetTitle}>Price</div>
              <div className={styles.chips}>
                {facets.prices.map((p) => (
                  <button
                    key={p}
                    type="button"
                    className={`${styles.chip} ${priceLevels.includes(p) ? styles.chipActive : ''}`}
                    onClick={() => toggle(priceLevels, setPriceLevels, p)}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
          )}

          {facets && facets.categories.length > 0 && (
            <div className={styles.facetGroup}>
              <div className={styles.facetTitle}>Category</div>
              <div className={styles.chips}>
                {facets.categories.map((c) => (
                  <button
                    key={c}
                    type="button"
                    className={`${styles.chip} ${categories.includes(c) ? styles.chipActive : ''}`}
                    onClick={() => toggle(categories, setCategories, c)}
                  >
                    {c}
                  </button>
                ))}
              </div>
            </div>
          )}

          {hasFilters && (
            <button type="button" className={styles.clearFilters} onClick={clearFilters}>
              Clear filters
            </button>
          )}
        </aside>

        <div className={styles.results}>
          <div className={styles.summary}>
            {headline}
            {activeType && !isBrowse && <> · Type: {domain.pluralOf(activeType)}</>}
            {!loading && <> · {items.length}{offset ? '+' : ''} results</>}
          </div>

          {loading ? (
            <div className={styles.skeletons}>
              {Array.from({ length: 6 }).map((_, i) => (
                <SkeletonBlock key={i} height="3.5rem" />
              ))}
            </div>
          ) : error ? (
            <div className={styles.message}>
              {error === 'network'
                ? COPY.states.connectionError
                : COPY.states.errorNetwork}
            </div>
          ) : items.length === 0 ? (
            <div className={styles.message}>
              {isBrowse ? 'No entries of this type yet.' : COPY.states.noResults(q)}
            </div>
          ) : (
            <>
              {items.map((e) => (
                <ResultCard
                  key={e.pointId}
                  entity={e}
                  score={e.score}
                  onClick={() => openEntity(e)}
                />
              ))}
              {offset != null && (
                <button
                  type="button"
                  className={styles.more}
                  onClick={() => load(false)}
                  disabled={loadingMore}
                >
                  {loadingMore ? 'Loading…' : 'Load more'}
                </button>
              )}
            </>
          )}

          <div className={styles.mechanism}>
            {near
              ? `Ranked by distance from ${near} (haversine)${
                  hasFilters ? ' + filters' : ''
                }.`
              : isBrowse
                ? `Browsing${hasFilters ? ' + filters (location / rating / price / category / amenities)' : ''}.`
                : `Search: in-browser semantic vector query${
                    hasFilters ? ' + filters (location / rating / price / category / amenities)' : ''
                  }.`}
          </div>
        </div>
      </div>
    </div>
  );
}
