import { useState } from 'react';
import { useAuth } from '../lib/auth';
import styles from './AuthButton.module.css';

// Sign-in entry point. Login is OPTIONAL — discovery never requires it; this only
// unlocks actions (claiming a profile, tipping). When signed out it's a "Sign in"
// button; when signed in it's an account chip with a logout menu. The button shows
// immediately for anonymous visitors; clicking it lazy-loads Privy (see lib/auth).
export default function AuthButton() {
  const { authenticated, user, login, logout } = useAuth();
  const [open, setOpen] = useState(false);

  if (!authenticated) {
    return (
      <button type="button" className={styles.signIn} onClick={login} title="Sign in with email">
        Sign in
      </button>
    );
  }

  const email = user?.email?.address ?? null;
  const label = email ?? 'Account';
  const initial = (email ?? '?').charAt(0).toUpperCase();

  return (
    <div className={styles.wrap}>
      <button
        type="button"
        className={styles.chip}
        onClick={() => setOpen((v) => !v)}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className={styles.avatar}>{initial}</span>
      </button>
      {open && (
        <>
          <div className={styles.backdrop} onClick={() => setOpen(false)} aria-hidden="true" />
          <div className={styles.menu} role="menu">
            <div className={styles.email}>{label}</div>
            <button
              type="button"
              className={styles.menuItem}
              onClick={() => {
                setOpen(false);
                void logout();
              }}
            >
              Log out
            </button>
          </div>
        </>
      )}
    </div>
  );
}
