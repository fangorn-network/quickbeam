import { COPY } from '../lib/copy';
import { communityChip } from '../lib/community';
import StatusBadge from './StatusBadge';
import AuthButton from './AuthButton';
import styles from './TopBar.module.css';

interface Props {
  onCmdK: () => void;
  onBack: () => void;
  canGoBack: boolean;
  connectionError?: boolean;
  onToggleTheme: () => void;
  onMenu?: () => void;
  onHome?: () => void;
  tripCount?: number;
  onTrip?: () => void;
}

export default function TopBar({
  onCmdK,
  onBack,
  canGoBack,
  connectionError,
  onToggleTheme,
  onMenu,
  onHome,
  tripCount = 0,
  onTrip,
}: Props) {
  return (
    <header className={`${styles.bar} ${connectionError ? styles.errored : ''}`}>
      <div className={styles.left}>
        <button
          type="button"
          className={styles.menuBtn}
          onClick={onMenu}
          aria-label="Menu"
          title="Menu"
        >
          ☰
        </button>
        <button
          type="button"
          className={styles.backBtn}
          onClick={onBack}
          disabled={!canGoBack}
          aria-label="Back"
          title="Back"
        >
          ←
        </button>
        <button
          type="button"
          className={styles.logo}
          onClick={onHome}
          aria-label="Home"
          title="Home"
        >
          <span className={styles.mark}>◇</span>
          <span className={styles.logoText}>SOND3R</span>
        </button>
        <span className={styles.divider} aria-hidden="true" />
        <button
          type="button"
          className={styles.community}
          onClick={onHome}
          title={`${communityChip} — home`}
        >
          {communityChip}
        </button>
      </div>

      <button type="button" className={styles.ghost} onClick={onCmdK}>
        <span className={styles.ghostIcon}>⌕</span>
        {COPY.cmdk.ghost}
      </button>

      <div className={styles.right}>
        {connectionError && (
          <StatusBadge variant="error" label={COPY.states.connectionError} />
        )}
        <button
          type="button"
          className={styles.trip}
          onClick={onTrip}
          aria-label="My trip"
          title="My trip"
        >
          🧭 Trip
          {tripCount > 0 && <span className={styles.tripCount}>{tripCount}</span>}
        </button>
        <button
          type="button"
          className={styles.gear}
          onClick={onToggleTheme}
          aria-label="Toggle theme"
          title="Toggle light / dark"
        >
          ◐
        </button>
        <AuthButton />
      </div>
    </header>
  );
}
