import { useState } from 'react';
import { useAuth } from '../lib/auth';
import { useClaim, shortAddr } from '../lib/claims';
import SchemaForm from './SchemaForm';
import { getSchema } from '../lib/schemas';
import { CLAIMS_ENABLED } from '../lib/config';
import styles from './ProfileOwnership.module.css';

interface Props {
  placeId: string | null;
  owner?: string | null;
  /** The listing's fields, used to pre-fill the claim/profile form. */
  fields?: Record<string, unknown>;
}

// The ownership / provenance strip on a Business profile. Two states:
//   • claimed   — a verified owner badge (+ tip, once Phase 4 ships)
//   • unclaimed — provenance ("Listed by …") + a "claim this profile" call-to-action
// The claim flow itself is Phase 3/4 (login + on-chain registry); for now the CTA
// opens an honest explainer rather than faking a transaction.
export default function ProfileOwnership({ placeId, owner, fields }: Props) {
  const claim = useClaim(placeId);
  const { authenticated, login } = useAuth();
  const [claiming, setClaiming] = useState(false);

  // Hard off-switch for the claim/ownership UI (VITE_CLAIMS=off). Placed after the
  // hooks so their call order stays stable (Rules of Hooks).
  if (!CLAIMS_ENABLED) return null;

  // The write target for a claim: the BusinessProfile schema, pre-filled from this
  // listing so the owner edits rather than re-types. Publishing it runs the full
  // pipeline (on-chain → watcher embeds → CDN delta shard).
  const profileSchema = getSchema('fangorn.places.businessprofile.v0');
  const prefill = profileSchema?.prefillFrom
    ? profileSchema.prefillFrom({ ...(fields ?? {}), placeId: placeId ?? '' })
    : undefined;

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
          <button type="button" className={styles.claimBtn} onClick={() => setClaiming((v) => !v)}>
            {claiming ? 'Cancel' : 'Claim this profile'}
          </button>
        ) : (
          <button type="button" className={styles.claimBtn} onClick={login}>
            Sign in to claim →
          </button>
        )}
      </div>
      {claiming && profileSchema && (
        <div className={styles.info}>
          <p className={styles.lead}>
            Publish an owner-authored profile for this place. It’s signed with your
            wallet and recorded on-chain — only this record is public, never what you
            search or browse.
          </p>
          <SchemaForm
            schema={profileSchema}
            prefill={prefill}
            compact
            onDone={() => setClaiming(false)}
          />
        </div>
      )}
    </div>
  );
}
