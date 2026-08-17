import { useNavigate } from 'react-router-dom';
import SearchBar from '../components/SearchBar';
import TypeBrowseGrid from '../components/TypeBrowseGrid';
import EntityBadge from '../components/EntityBadge';
import type { EntityType, PageRef } from '../lib/types';
import { COPY } from '../lib/copy';
import { COMMUNITY, communityFull } from '../lib/community';
import { useDomain } from '../lib/domainContext';
import { browseHref, searchHref, searchPageRef } from '../lib/nav';
import { useBackStack } from '../hooks/useBackStack';
import styles from './Landing.module.css';

interface Props {
  counts: Partial<Record<EntityType, number | null>>;
  onVisit: (p: PageRef) => void;
}

export default function Landing({ counts, onVisit }: Props) {
  const navigate = useNavigate();
  const domain = useDomain();
  const { recent } = useBackStack();

  function onSearch(q: string, type?: EntityType) {
    if (!q) return;
    onVisit(searchPageRef(q, type));
    navigate(searchHref(q, type));
  }

  return (
    <div className={styles.page}>
      {/* Branded community hero */}
      <section className={styles.hero}>
        <div className={styles.eyebrow}>
          <span className={styles.eyebrowMark}>◇</span> {COPY.landing.eyebrow} · {communityFull}
        </div>
        <h1 className={styles.title}>{COPY.landing.discover(COMMUNITY.name)}</h1>
        <p className={styles.tagline}>{COMMUNITY.tagline}</p>

        <div className={styles.searchArea}>
          <SearchBar onSearch={onSearch} />
        </div>

        <div className={styles.pills}>
          {domain.entityTypes.map((t) => (
            <button
              key={t}
              type="button"
              className={styles.pill}
              onClick={() => navigate(browseHref(t))}
              style={{ '--accent': domain.accentColor(t) } as React.CSSProperties}
            >
              <span className={styles.pillCount}>{counts[t] ?? '—'}</span>
              {domain.pluralOf(t)}
            </button>
          ))}
        </div>
      </section>

      <h2 className={styles.h2}>{COPY.browse.heading}</h2>
      <TypeBrowseGrid
        typeCounts={counts}
        onTypeSelect={(t) => navigate(browseHref(t))}
      />

      <h2 className={styles.h2}>{COPY.browse.recentHeading}</h2>
      {recent.length === 0 ? (
        <div className={styles.empty}>{COPY.browse.recentEmpty}</div>
      ) : (
        <ul className={styles.recent}>
          {recent.map((p) => (
            <li key={p.href}>
              <button
                type="button"
                className={styles.recentRow}
                onClick={() => navigate(p.href)}
              >
                {p.entityType && p.kind === 'entity' && (
                  <EntityBadge type={p.entityType} size="sm" />
                )}
                <span className={styles.recentLabel}>{p.label}</span>
                <span className={styles.recentKind}>{p.kind}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Roadmap teaser — the "claim your business" vision. */}
      <div className={styles.claim}>
        <span className={styles.claimDot} aria-hidden="true">◇</span>
        {COPY.landing.claimPrompt(COMMUNITY.name)}{' '}
        <br></br>
        <br></br>
        <strong>{COPY.landing.claimSoon}</strong>
        <br></br>
        <br></br>
        <strong>{COPY.landing.contact} <a href='mailto:fangorn@fangorn.network'>fangorn@fangorn.network</a></strong>
      </div>
    </div>
  );
}
