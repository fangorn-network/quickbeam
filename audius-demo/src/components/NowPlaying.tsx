import { duration as fmt, fallbackArt, initial, recArt } from '../lib/format';
import { usePlayer } from '../lib/player';
import { goEntity } from '../lib/router';
import Chip from './Chip';
import PlayButton from './PlayButton';
import Taste from './Taste';

/**
 * The persistent bottom bar — Audius' signature chrome, and the reason playback
 * belongs in an Audius-branded demo rather than being a nice-to-have.
 *
 * It carries the publisher chip, so whatever is playing continuously states which
 * wallet published it. That is the demo's whole argument, sitting in the corner of the
 * screen while the room is looking at something else.
 */
export default function NowPlaying() {
  const { current, time, duration, seek, next, prev, unavailable } = usePlayer();
  if (!current) return null;

  const art = recArt(current, 150);
  const dead = unavailable.has(current.id);
  const max = duration || Number(current.fields.duration) || 0;

  return (
    <div className="nowplaying" role="region" aria-label="Now playing">
      <div className="np-art" onClick={() => goEntity(current.id)}>
        {art
          ? <img src={art} alt="" />
          : <div className="art-fallback" style={{ background: fallbackArt(current.id) }}>
              {initial(current.fields.title)}
            </div>}
      </div>

      <div className="np-meta">
        <button className="np-title" onClick={() => goEntity(current.id)}>
          {current.fields.title || current.id}
        </button>
        <div className="np-sub">
          <span>{current.fields.artist}</span>
          <Chip owner={current.owner} />
        </div>
      </div>

      <div className="np-controls">
        <button className="np-step" onClick={prev} aria-label="Previous track">‹‹</button>
        <PlayButton rec={current} size="lg" />
        <button className="np-step" onClick={next} aria-label="Next track">››</button>
        <Taste rec={current} />
      </div>

      <div className="np-scrub">
        <span className="np-time">{fmt(time)}</span>
        {/* A real range input, so the scrubber is keyboard-operable for free. */}
        <input
          type="range"
          min={0}
          max={max || 1}
          step={1}
          value={Math.min(time, max || 1)}
          onChange={(e) => seek(Number(e.target.value))}
          aria-label="Seek"
          style={{ ['--pct' as string]: `${max ? (time / max) * 100 : 0}%` }}
        />
        <span className="np-time">{fmt(max)}</span>
      </div>

      {dead && <span className="np-dead">Not available to stream</span>}
    </div>
  );
}
