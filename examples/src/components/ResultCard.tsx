import { useDomain } from '../lib/domainContext';
import type { EntitySummary } from '../lib/types';
import { parseHours, isOpenNow } from '../lib/labels';
import Icon from './Icon';
import type { IconName } from './Icon';
import styles from './ResultCard.module.css';

interface Props {
  entity: EntitySummary;
  score?: number;
  onClick: () => void;
  highlighted?: boolean;
  featured?: boolean;
  // The concierge's grounded "why it fits" line (from lib/llm). `explaining`
  // shows a typing caret while it streams in.
  explanation?: string;
  explaining?: boolean;
}

// Quick at-a-glance pills derived from the entity's own fields: open-now & rating
// for places, date & free for events. Mirrors the entity-page header logic so a
// list scan answers "open?" / "when?" without a click.
function cardBadges(entity: EntitySummary): { key: string; label: string; tone: 'open' | 'closed' | 'neutral' }[] {
  const f = entity.fields as Record<string, unknown>;
  const out: { key: string; label: string; tone: 'open' | 'closed' | 'neutral' }[] = [];
  if (entity.entityType === 'Event') {
    const when = (typeof f.dateLabel === 'string' && f.dateLabel)
      || (typeof f.startDate === 'string' && f.startDate.slice(0, 10)) || '';
    if (when) out.push({ key: 'when', label: when, tone: 'neutral' });
    if (f.isFree === true) out.push({ key: 'free', label: 'Free', tone: 'open' });
    if (f.isPast === true) out.push({ key: 'past', label: 'Past', tone: 'closed' });
  } else {
    const status = typeof f.businessStatus === 'string' ? f.businessStatus : '';
    const closedStatus = status && status !== 'OPERATIONAL';
    const hoursKey = Object.keys(f).find((k) => /hours/i.test(k) && typeof f[k] === 'string');
    const open = hoursKey ? isOpenNow(parseHours(f[hoursKey])) : null;
    if (closedStatus) {
      out.push({ key: 'status', label: /PERMANENT/i.test(status) ? 'Permanently closed' : 'Temporarily closed', tone: 'closed' });
    } else if (open != null) {
      out.push({ key: 'open', label: open ? 'Open now' : 'Closed', tone: open ? 'open' : 'closed' });
    }
    if (typeof f.rating === 'number') out.push({ key: 'rating', label: `★ ${f.rating.toFixed(1)}`, tone: 'neutral' });
    if (typeof f.priceLevel === 'string' && f.priceLevel) out.push({ key: 'price', label: f.priceLevel, tone: 'neutral' });
  }
  return out;
}

// A faint topographic contour wash — the signature texture for every photo slot.
function Contour() {
  return (
    <svg className={styles.contour} viewBox="0 0 200 120" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
      {[8, 22, 38, 56, 76, 98].map((r, i) => (
        <path
          key={i}
          d={`M${140 - r * 0.4} ${30 + i * 2}
              q ${r} ${-r * 0.7} ${r * 1.6} ${r * 0.2}
              t ${r * 1.2} ${r * 0.5}`}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        />
      ))}
    </svg>
  );
}

export default function ResultCard({ entity, score, onClick, highlighted, featured, explanation, explaining }: Props) {
  const domain = useDomain();
  const secondary = domain.secondaryLine(entity);
  const isEvent = entity.entityType === 'Event';
  const tags = domain.primaryTags(entity.fields as Record<string, unknown>).slice(0, featured ? 4 : 3);
  const badges = cardBadges(entity);
  const slotIcon: IconName = isEvent ? 'music' : 'glass';
  // Score is a cosine similarity; surface it as a friendly "vibe match" reading.
  const pct = score != null ? Math.max(0, Math.min(100, Math.round(score * 100))) : null;

  return (
    <button
      type="button"
      className={[
        styles.card,
        isEvent ? styles.event : styles.place,
        featured ? styles.featured : '',
        highlighted ? styles.highlighted : '',
      ].join(' ')}
      onClick={onClick}
    >
      <div className={styles.slot}>
        <Contour />
        <Icon name={slotIcon} size={featured ? 40 : 30} className={styles.slotIcon} />
        <span className={styles.kind}>
          <Icon name={isEvent ? 'calendar' : 'pin'} size={12} />
          {domain.typeMeta(entity.entityType).singular}
        </span>
      </div>

      <div className={styles.body}>
        <h3 className={styles.title}>{entity.title}</h3>
        {secondary && <div className={styles.secondary}>{secondary}</div>}

        {(tags.length > 0 || badges.length > 0) && (
          <div className={styles.pills}>
            {badges.map((b) => (
              <span key={b.key} className={`${styles.pill} ${b.tone !== 'neutral' ? styles[b.tone] : ''}`}>
                {b.label}
              </span>
            ))}
            {tags.map((t) => (
              <span key={t} className={`${styles.pill} ${styles.tag}`}>{t}</span>
            ))}
          </div>
        )}

        {(explanation || explaining) && (
          <div className={styles.insight}>
            <Icon name="sparkle" size={13} className={styles.insightIcon} />
            <p className={styles.insightText}>
              {explanation}
              {explaining && <span className={styles.insightCaret} aria-hidden="true" />}
            </p>
          </div>
        )}

        {pct != null && (
          <div className={styles.match} title="How closely this matches your search">
            <div className={styles.matchHead}>
              <span className={styles.matchLabel}>Vibe match</span>
              <span className={styles.matchPct}>{pct}%</span>
            </div>
            <div className={styles.meter}>
              <span className={styles.meterFill} style={{ width: `${pct}%` }} />
            </div>
          </div>
        )}
      </div>
    </button>
  );
}
