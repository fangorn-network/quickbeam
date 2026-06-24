import { useState } from 'react';
import { useAuth } from '../lib/auth';
import { useClaim, shortAddr } from '../lib/claims';
import styles from './ProfileOwnership.module.css';

interface Props {
  placeId: string | null;
  owner?: string | null;
}

// The ownership / provenance strip on a Business profile. Two states:
//   • claimed   — a verified owner badge (+ tip, once Phase 4 ships)
//   • unclaimed — provenance ("Listed by …") + a "claim this profile" call-to-action
// The claim flow itself is Phase 3/4 (login + on-chain registry); for now the CTA
// opens an honest explainer rather than faking a transaction.
export default function ProfileOwnership({ placeId, owner }: Props) {
  const claim = useClaim(placeId);
  const { authenticated, login } = useAuth();
  const [showInfo, setShowInfo] = useState(false);

  if (claim.claimed) {
    return (
      <div className={`${styles.strip} ${styles.claimed}`}>
        <span className={styles.badge}>✓ Claimed</span>
        <span className={styles.text}>
          Verified by owner
          {claim.claimant ? ` · ${shortAddr(claim.claimant)}` : ''}
        </span>
      </div>
    );
  }

  return (
    <div className={styles.strip}>
      <div className={styles.row}>
        <span className={styles.text}>
          {owner ? (
            <>
              Listed by{' '}
              <code className={styles.addr} title={owner}>
                {shortAddr(owner)}
              </code>
            </>
          ) : (
            'Unclaimed profile'
          )}
        </span>
        {authenticated ? (
          <button type="button" className={styles.claimBtn} onClick={() => setShowInfo((v) => !v)}>
            Claim this profile
          </button>
        ) : (
          <button type="button" className={styles.claimBtn} onClick={login}>
            Sign in to claim →
          </button>
        )}
      </div>
      {showInfo && (
        <div className={styles.info}>
          You're signed in — claiming will verify ownership and record it on Base (a
          public registry mapping this place to your address), after which you can
          receive tips. That on-chain step ships next. Nothing you search or browse is
          ever part of it — only the claim itself is public.
        </div>
      )}
    </div>
  );
}
