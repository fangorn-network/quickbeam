import { kernelReset, type KernelSnapshot } from '../lib/client';

/**
 * A readout for the session kernel.
 *
 * It reframes values the kernel already computes — it fetches nothing and mutates
 * nothing except the reset button. The point is legibility: a CTO can see this is a
 * geometric model with state, running locally over the downloaded snapshot, rather
 * than a black box calling somebody's recommendation API.
 *
 * Adapted from sonder's KernelStrip; same readings, our palette.
 */
export default function KernelStrip({
  snap, onReset,
}: { snap: KernelSnapshot; onReset: () => void }) {
  if (!snap.timestep && !snap.nSkips) return null;

  const suppressed = snap.blacklisted.length + snap.muted.length;
  const tags = [...snap.topGenres, ...snap.topMoods].slice(0, 4).map(([t]) => t);

  return (
    <div className="kstrip" role="status" aria-label="Session model">
      <span className="kstrip-label">Session model</span>

      {/* Speed and lookahead are the Markov part: where you are vs where you're
          heading. Meter widths are normalised only for display. */}
      <Readout label="speed" value={snap.speed.toFixed(2)} meter={Math.min(snap.speed, 1)} />
      <Readout label="lookahead" value={snap.lookahead.toFixed(2)} meter={snap.lookahead / 0.4} />
      <Readout label="spread" value={snap.spread.toFixed(2)} meter={snap.spread} />
      <Readout label="entropy" value={snap.entropy.toFixed(2)} meter={snap.entropy} />

      <span className="kstrip-sep" aria-hidden="true" />

      <span className="kstrip-item">
        <span className="kstrip-k">signals</span>
        <span className="kstrip-v">{snap.timestep}▲ {snap.nSkips}▼</span>
      </span>

      {tags.length > 0 && (
        <span className="kstrip-item kstrip-tags">
          <span className="kstrip-k">taste</span>
          <span className="kstrip-v">{tags.join(' · ')}</span>
        </span>
      )}

      {suppressed > 0 && (
        <span className="kstrip-item" title={`${snap.blacklisted.length} suppressed, ${snap.muted.length} rested`}>
          <span className="kstrip-k">suppressed</span>
          <span className="kstrip-v">{suppressed}</span>
        </span>
      )}

      <button
        className="kstrip-reset"
        onClick={() => { void kernelReset().then(onReset); }}
        title="Forget this session and start from nothing"
      >
        Reset
      </button>
    </div>
  );
}

function Readout({ label, value, meter }: { label: string; value: string; meter: number }) {
  const pct = Math.max(0, Math.min(1, meter)) * 100;
  return (
    <span className="kstrip-item">
      <span className="kstrip-k">{label}</span>
      <span className="kstrip-v">{value}</span>
      <span className="kstrip-meter" aria-hidden="true"><i style={{ width: `${pct}%` }} /></span>
    </span>
  );
}
