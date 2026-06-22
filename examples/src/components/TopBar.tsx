import { COPY } from '../lib/copy';
import StatusBadge from './StatusBadge';
import styles from './TopBar.module.css';

interface Props {
  onCmdK: () => void;
  onBack: () => void;
  canGoBack: boolean;
  connectionError?: boolean;
  onToggleTheme: () => void;
}

export default function TopBar({
  onCmdK,
  onBack,
  canGoBack,
  connectionError,
  onToggleTheme,
}: Props) {
  return (
    <header className={`${styles.bar} ${connectionError ? styles.errored : ''}`}>
      <div className={styles.left}>
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
        <span className={styles.logo}>▣ schema-browser</span>
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
          className={styles.gear}
          onClick={onToggleTheme}
          aria-label="Toggle theme"
          title="Toggle theme"
        >
          ⚙
        </button>
      </div>
    </header>
  );
}
