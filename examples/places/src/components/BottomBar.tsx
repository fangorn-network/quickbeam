import styles from './BottomBar.module.css';

interface Props {
  onDiscover: () => void;
  onExplore: () => void;
  onTrip: () => void;
  onToggleTheme: () => void;
  tripCount?: number;
}

// Mobile-only primary navigation. On phones the TopBar's right-hand actions don't
// fit, so they live here as a fixed bottom bar (hidden on desktop via CSS). Each
// tab is an icon over a label so the targets stay tappable without truncating.
export default function BottomBar({
  onDiscover,
  onExplore,
  onTrip,
  onToggleTheme,
  tripCount = 0,
}: Props) {
  return (
    <nav className={styles.bar} aria-label="Primary">
      <button type="button" className={styles.tab} onClick={onDiscover}>
        <span className={styles.icon} aria-hidden="true">⌖</span>
        <span className={styles.label}>Discover</span>
      </button>
      <button type="button" className={styles.tab} onClick={onExplore}>
        <span className={styles.icon} aria-hidden="true">✦</span>
        <span className={styles.label}>Explore</span>
      </button>
      <button type="button" className={styles.tab} onClick={onTrip}>
        <span className={styles.icon} aria-hidden="true">
          🧭
          {tripCount > 0 && <span className={styles.count}>{tripCount}</span>}
        </span>
        <span className={styles.label}>Trip</span>
      </button>
      <button type="button" className={styles.tab} onClick={onToggleTheme}>
        <span className={styles.icon} aria-hidden="true">◐</span>
        <span className={styles.label}>Theme</span>
      </button>
    </nav>
  );
}
