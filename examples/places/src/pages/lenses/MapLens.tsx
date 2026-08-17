// Map lens — the shared result pool on a geographic map, pins synced to a ranked
// list. Renders the same `items` Discover fetched (the coordinate-bearing subset),
// so flipping here from List is instant and consistent. A `focus` entity arriving
// from its own page may sit outside the pool, so we fetch it directly and prepend.
import { useEffect, useMemo, useRef, useState } from 'react';
import MapView from '../../components/MapView';
import ResultCard from '../../components/ResultCard';
import SkeletonBlock from '../../components/SkeletonBlock';
import { useDomain } from '../../lib/domainContext';
import { getPoint, toSummary } from '../../lib/qdrant';
import { parseCoords } from '../../lib/geo';
import type { EntitySummary } from '../../lib/types';
import type { LensProps } from './types';
import styles from '../Map.module.css';

interface Props extends LensProps {
  focus?: string;
}

export default function MapLens({ items, loading, focus, onOpen }: Props) {
  const domain = useDomain();
  const [hovered, setHovered] = useState<string | null>(null);
  const [extra, setExtra] = useState<EntitySummary | null>(null);
  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const listRef = useRef<HTMLElement>(null);

  // The map only speaks coordinates — keep just what we can plot. Memoized so its
  // identity is stable across renders; otherwise the focus effect below (which
  // depends on it) would re-run every render and loop on setExtra.
  const plotted = useMemo(
    () => items.filter((e) => parseCoords(e.fields?.coordinates)),
    [items],
  );

  // A focused entity (arriving from its own page) may not be in the pool — fetch
  // it directly so it's always present to fly to and ring. Guard against re-fetch:
  // once we hold the focused entity (or it's in the pool), do nothing — otherwise
  // setExtra → re-render → fetch → setExtra loops until React aborts the tree.
  useEffect(() => {
    if (!focus || plotted.some((e) => e.pointId === focus)) {
      setExtra(null);
      return;
    }
    if (extra?.pointId === focus) return; // already fetched it
    let live = true;
    getPoint(focus)
      .then((p) => {
        const s = toSummary(p);
        if (live && parseCoords(s.fields?.coordinates)) setExtra(s);
      })
      .catch(() => {/* not found — nothing to focus */});
    return () => {
      live = false;
    };
  }, [focus, plotted, extra]);

  const list = extra ? [extra, ...plotted] : plotted;

  // Ring the focused pin once it's present.
  useEffect(() => {
    if (focus) setHovered(focus);
  }, [focus]);

  // When the map hover changes, bring the matching card into view — but scroll
  // ONLY the list, never the page. (scrollIntoView bubbles to every scrollable
  // ancestor, so on landing it would otherwise yank the whole surface down to a
  // low-ranked focused pin. On mobile the list isn't its own scroller, so this
  // is a no-op there, which is the behaviour we want.)
  useEffect(() => {
    if (!hovered) return;
    const card = cardRefs.current[hovered];
    const list = listRef.current;
    if (!card || !list || list.scrollHeight <= list.clientHeight) return;
    const cardTop = card.offsetTop - list.offsetTop;
    const target = cardTop - list.clientHeight / 2 + card.clientHeight / 2;
    list.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
  }, [hovered]);

  return (
    <div className={styles.split}>
      <div className={styles.mapWrap}>
        <MapView
          items={list}
          accentOf={(t) => domain.accentColor(t)}
          onOpen={onOpen}
          activeId={hovered}
          onHover={setHovered}
          focusId={focus || null}
        />
      </div>

      <aside className={styles.list} ref={listRef}>
        <div className={styles.listHead}>{loading ? 'Mapping…' : `${list.length} on the map`}</div>
        {loading ? (
          Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className={styles.skel}>
              <SkeletonBlock height="100%" />
            </div>
          ))
        ) : list.length === 0 ? (
          <div className={styles.empty}>Nothing with a location to plot here yet.</div>
        ) : (
          list.map((e) => (
            <div
              key={e.pointId}
              ref={(el) => {
                cardRefs.current[e.pointId] = el;
              }}
              className={`${styles.cardWrap} ${hovered === e.pointId ? styles.cardActive : ''}`}
              onMouseEnter={() => setHovered(e.pointId)}
              onMouseLeave={() => setHovered(null)}
            >
              <ResultCard entity={e} score={e.score} onClick={() => onOpen(e)} />
            </div>
          ))
        )}
      </aside>
    </div>
  );
}
