import { Link } from 'react-router-dom';
import { COMMUNITY, communityFull } from '../lib/community';
import styles from './Footer.module.css';

const FANGORN_URL = 'https://fangorn.network';
const FANGORN_EMAIL = 'fangorn@fangorn.network';

const EXPLORE: { to: string; label: string }[] = [
  { to: '/discover', label: 'Discover' },
  { to: '/discover?lens=map', label: 'Map' },
  { to: '/atlas', label: 'Atlas' },
  { to: '/trip', label: 'Liked places' },
];

export default function Footer() {
  const year = new Date().getFullYear();
  return (
    <footer className={styles.footer}>
      <div className={styles.inner}>
        <div className={styles.cols}>
          <div className={styles.brandCol}>
            <div className={styles.brand}>
              <span className={styles.mark}>◇</span>
              <span className={styles.wordmark}>SOND3R</span>
            </div>
            <div className={styles.community}>{communityFull}</div>
            <p className={styles.blurb}>{COMMUNITY.blurb}</p>
          </div>

          <div className={styles.linkCol}>
            <div className={styles.colTitle}>Explore</div>
            {EXPLORE.map((l) => (
              <Link key={l.to} className={styles.link} to={l.to}>
                {l.label}
              </Link>
            ))}
          </div>

          <div className={styles.linkCol}>
            <div className={styles.colTitle}>Made by Fangorn</div>
            <a className={styles.link} href={FANGORN_URL} target="_blank" rel="noreferrer">
              fangorn.network <span className={styles.arrow} aria-hidden="true">↗</span>
            </a>
            <a className={styles.link} href={`mailto:${FANGORN_EMAIL}`}>
              Contact us
            </a>
          </div>
        </div>

        <div className={styles.legal}>
          <span>© {year} Fangorn</span>
          <span className={styles.powered}>
            <span className={styles.markSm} aria-hidden="true">◇</span>
            Semantic search powered by{' '}
            <a className={styles.poweredLink} href={FANGORN_URL} target="_blank" rel="noreferrer">
              Fangorn
            </a>
          </span>
        </div>
      </div>
    </footer>
  );
}
