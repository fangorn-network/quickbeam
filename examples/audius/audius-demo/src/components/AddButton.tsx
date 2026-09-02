import { isPlayable } from '../lib/player';
import { usePlaylists } from '../lib/playlists.tsx';
import type { Rec } from '../lib/types';

/**
 * `+` — opens the one shared picker (see playlists.tsx).
 *
 * Only for playable records: "add to a playlist" on an Artist or a Genre would be an
 * offer this app can't keep, so it reuses the player's own predicate rather than
 * inventing a second definition of what a track is.
 */
export default function AddButton({ rec, size = 'md' }: { rec: Rec; size?: 'sm' | 'md' }) {
  const { addTo, playlists } = usePlaylists();
  if (!isPlayable(rec)) return null;
  const saved = playlists.some((p) => p.trackIds.includes(rec.id));

  return (
    <button
      className={`taste-btn addbtn addbtn-${size}${saved ? ' is-on' : ''}`}
      aria-label={`Add ${rec.fields.title ?? 'this track'} to a playlist`}
      aria-pressed={saved}
      title={saved ? 'In a playlist — add to another' : 'Add to a playlist'}
      // .card is itself a <button> that navigates (Card.tsx), so this has to stop
      // here — same reason PlayButton and Taste do.
      onClick={(e) => { e.stopPropagation(); e.preventDefault(); addTo(rec); }}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
      </svg>
    </button>
  );
}
