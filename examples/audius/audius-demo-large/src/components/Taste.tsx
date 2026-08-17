import { useKernel } from '../lib/kernel';
import type { Rec } from '../lib/types';

/**
 * Explicit like / dislike.
 *
 * These feed the SAME transitions as play and skip — there is one state machine, and
 * a thumb is just an unambiguous instance of the same evidence. Their real value is
 * control: on stage you can steer the session deliberately instead of waiting for it
 * to drift somewhere worth showing.
 */
export default function Taste({ rec, size = 'md' }: { rec: Rec; size?: 'sm' | 'md' }) {
  const { signal, liked, disliked } = useKernel();
  const isLiked = liked.has(rec.id);
  const isDisliked = disliked.has(rec.id);

  return (
    <span className={`taste taste-${size}`}>
      <button
        className={`taste-btn${isLiked ? ' is-on' : ''}`}
        aria-label={`More like ${rec.fields.title ?? 'this'}`}
        aria-pressed={isLiked}
        title="More like this"
        onClick={(e) => { e.stopPropagation(); e.preventDefault(); signal(rec.id, 'like'); }}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 20.3l-1.5-1.35C5.4 14.36 2 11.28 2 7.5 2 4.42 4.42 2 7.5 2c1.74 0 3.41.81 4.5 2.09C13.09 2.81 14.76 2 16.5 2 19.58 2 22 4.42 22 7.5c0 3.78-3.4 6.86-8.5 11.45z"
                fill={isLiked ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.8" />
        </svg>
      </button>
      <button
        className={`taste-btn${isDisliked ? ' is-off' : ''}`}
        aria-label={`Less like ${rec.fields.title ?? 'this'}`}
        aria-pressed={isDisliked}
        title="Less like this"
        onClick={(e) => { e.stopPropagation(); e.preventDefault(); signal(rec.id, 'dislike'); }}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 12h12" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
        </svg>
      </button>
    </span>
  );
}
