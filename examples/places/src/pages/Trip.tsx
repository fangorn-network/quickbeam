import { useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import Breadcrumb from '../components/Breadcrumb';
import EntityBadge from '../components/EntityBadge';
import { useTrip, sharedTripFromHash } from '../lib/trip';
import type { TripItem } from '../lib/trip';
import { entityHref } from '../lib/nav';
import { copyText } from '../lib/clipboard';
import styles from './Trip.module.css';

export default function Trip() {
  const navigate = useNavigate();
  const location = useLocation();
  const trip = useTrip();

  // A trip shared via `#trip=…` is rendered read-only with a "Save" action — we
  // never clobber the visitor's own trip silently.
  const shared = useMemo(() => sharedTripFromHash(location.hash), [location.hash]);

  if (shared && shared.length > 0) {
    return <SharedTrip items={shared} />;
  }

  const { items, remove, move, clear, shareUrl } = trip;

  return (
    <div className={styles.page}>
      <Breadcrumb crumbs={[{ label: 'Browse', href: '/' }, { label: 'My trip' }]} />
      <div className={styles.head}>
        <h1 className={styles.h1}>My trip</h1>
        {items.length > 0 && (
          <div className={styles.actions}>
            <ShareButton url={shareUrl()} />
            <button type="button" className={styles.ghostBtn} onClick={clear}>
              Clear
            </button>
          </div>
        )}
      </div>

      {items.length === 0 ? (
        <div className={styles.empty}>
          Nothing saved yet. Add items with <span className={styles.pin}>＋ Add to trip</span>{' '}
          and they'll collect here, then share the whole plan with a link. 
          Your trip details never leave your device unless you share.
        </div>
      ) : (
        <ol className={styles.list}>
          {items.map((it, i) => (
            <li key={it.id} className={styles.row}>
              <div className={styles.reorder}>
                <button
                  type="button"
                  className={styles.arrow}
                  disabled={i === 0}
                  onClick={() => move(it.id, -1)}
                  aria-label="Move up"
                >
                  ↑
                </button>
                <button
                  type="button"
                  className={styles.arrow}
                  disabled={i === items.length - 1}
                  onClick={() => move(it.id, 1)}
                  aria-label="Move down"
                >
                  ↓
                </button>
              </div>
              <button
                type="button"
                className={styles.item}
                onClick={() => navigate(entityHref(it.id))}
              >
                <EntityBadge type={it.type} size="sm" />
                <span className={styles.itemTitle}>{it.title}</span>
                <span className={styles.itemType}>{it.type}</span>
              </button>
              <button
                type="button"
                className={styles.remove}
                onClick={() => remove(it.id)}
                aria-label={`Remove ${it.title}`}
                title="Remove"
              >
                ✕
              </button>
            </li>
          ))}
        </ol>
      )}

      {items.length > 0 && (
        <div className={styles.note}>
          Your trip is stored only on this device. The share link carries it in the URL's{' '}
          <code>#</code> fragment — never sent to any server.
        </div>
      )}
    </div>
  );
}

// A trip opened from a share link: shown read-only, with a one-tap "Save to my trip".
function SharedTrip({ items }: { items: TripItem[] }) {
  const navigate = useNavigate();
  const trip = useTrip();
  const [saved, setSaved] = useState(false);

  function save() {
    for (const it of items) trip.add(it);
    setSaved(true);
  }

  return (
    <div className={styles.page}>
      <Breadcrumb crumbs={[{ label: 'Browse', href: '/' }, { label: 'Shared trip' }]} />
      <div className={styles.head}>
        <h1 className={styles.h1}>A trip was shared with you</h1>
        <div className={styles.actions}>
          {saved ? (
            <button type="button" className={styles.primaryBtn} onClick={() => navigate('/trip')}>
              Saved · open my trip
            </button>
          ) : (
            <button type="button" className={styles.primaryBtn} onClick={save}>
              ＋ Save to my trip
            </button>
          )}
        </div>
      </div>
      <ol className={styles.list}>
        {items.map((it) => (
          <li key={it.id} className={styles.row}>
            <button
              type="button"
              className={styles.item}
              onClick={() => navigate(entityHref(it.id))}
            >
              <EntityBadge type={it.type} size="sm" />
              <span className={styles.itemTitle}>{it.title}</span>
              <span className={styles.itemType}>{it.type}</span>
            </button>
          </li>
        ))}
      </ol>
      <div className={styles.note}>
        {items.length} stop{items.length === 1 ? '' : 's'}. Saving copies them into your own
        on-device trip — nothing is uploaded.
      </div>
    </div>
  );
}

function ShareButton({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  async function onShare() {
    const ok = await copyText(url);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    }
  }
  return (
    <button type="button" className={styles.primaryBtn} onClick={onShare}>
      {copied ? 'Link copied ✓' : '🔗 Copy share link'}
    </button>
  );
}
