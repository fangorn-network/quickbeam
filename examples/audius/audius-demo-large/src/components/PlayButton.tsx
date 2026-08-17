import { isPlayable, usePlayer } from '../lib/player';
import type { Rec } from '../lib/types';

/**
 * One play affordance, reused everywhere, so the four states (idle / loading /
 * playing / unavailable) can't drift between the card grid and the entity page.
 */
export default function PlayButton({
  rec, queue, size = 'md',
}: { rec: Rec; queue?: Rec[]; size?: 'sm' | 'md' | 'lg' }) {
  const { current, playing, loading, unavailable, play, toggle } = usePlayer();
  if (!isPlayable(rec)) return null;

  const isCurrent = current?.id === rec.id;
  const dead = unavailable.has(rec.id);
  const isPlaying = isCurrent && playing;
  const isLoading = isCurrent && loading;

  const label = dead ? 'Unavailable to stream'
    : isPlaying ? `Pause ${rec.fields.title ?? ''}`
      : `Play ${rec.fields.title ?? ''}`;

  return (
    <button
      className={`play play-${size}${isPlaying ? ' is-playing' : ''}${dead ? ' is-dead' : ''}`}
      aria-label={label}
      aria-pressed={isPlaying}
      title={dead ? "This track isn't available to stream" : label}
      disabled={dead}
      onClick={(e) => {
        // Cards are themselves buttons that navigate; playing must not also open
        // the entity page.
        e.stopPropagation();
        e.preventDefault();
        if (isCurrent) toggle();
        else play(rec, queue);
      }}
    >
      {dead ? <Slash /> : isLoading ? <span className="play-spin" /> : isPlaying ? <Pause /> : <Play />}
    </button>
  );
}

const Play = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor" /></svg>
);
const Pause = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M7.5 5h3.2v14H7.5zM13.3 5h3.2v14h-3.2z" fill="currentColor" />
  </svg>
);
const Slash = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 3.5a8.5 8.5 0 110 17 8.5 8.5 0 010-17zm0 2a6.5 6.5 0 00-5.2 10.4L16.4 6.8A6.47 6.47 0 0012 5.5zm0 13a6.5 6.5 0 005.2-10.4L7.6 17.2A6.47 6.47 0 0012 18.5z"
          fill="currentColor" />
  </svg>
);
