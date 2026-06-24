import { COMMUNITY, communityFull } from '../lib/community';
import styles from './Footer.module.css';

const FANGORN_URL = 'https://fangorn.network';
const FANGORN_EMAIL = 'fangorn@fangorn.network';

export default function Footer() {
  const year = new Date().getFullYear();
  return (
    <footer className={styles.footer}>
      <div className={styles.cols}>
        <div className={styles.brandCol}>
          <div className={styles.brand}>
            <span className={styles.mark}>◇</span> SOND3R
          </div>
          <div className={styles.community}>{communityFull}</div>
          <p className={styles.blurb}>{COMMUNITY.blurb}</p>
        </div>

        <div className={styles.linkCol}>
          <div className={styles.colTitle}>Made by Fangorn</div>
          <a className={styles.link} href={FANGORN_URL} target="_blank" rel="noreferrer">
            fangorn.network ↗
          </a>
          <a className={styles.link} href={`mailto:${FANGORN_EMAIL}`}>
            Contact us
          </a>
        </div>
      </div>

      <div className={styles.legal}>
        <span>© {year} Fangorn</span>
        <span className={styles.powered}>
          Semantic search powered by{' '}
          <a className={styles.poweredLink} href={FANGORN_URL} target="_blank" rel="noreferrer">
            Fangorn
          </a>
        </span>
      </div>
    </footer>
  );
}
