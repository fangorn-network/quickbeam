// HeadingRail — the ambient "where you're heading" strip. It surfaces the session
// kernel: a horizontal rail of places the corpus says your likes are drifting
// toward, re-populating as you like/dislike. No query, no spinner — it just reacts.
import { useHeadingRail } from '../lib/useHeadingRail';
import type { EntitySummary } from '../lib/types';
import Icon from './Icon';
import ResultCard from './ResultCard';
import styles from './HeadingRail.module.css';

export default function HeadingRail({ onOpen }: { onOpen: (e: EntitySummary) => void }) {
  const rail = useHeadingRail();
  if (!rail || rail.items.length === 0) return null;

  return (
    <section className={styles.rail} aria-label="Recommended for you">
      <div className={styles.head}>
        <span className={styles.title}>
          <Icon name="sparkle" size={15} /> Recommended for you
        </span>
        <span className={styles.sub}>Based on what you’ve liked</span>
      </div>
      <div className={styles.track}>
        {rail.items.map(({ entity, score }) => (
          <div key={entity.pointId} className={styles.cell}>
            <ResultCard entity={entity} score={score} onClick={() => onOpen(entity)} />
          </div>
        ))}
      </div>
    </section>
  );
}
