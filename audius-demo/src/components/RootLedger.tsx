import { shortAddr } from '../lib/format';
import { goEntity } from '../lib/router';
import type { Stats } from '../lib/types';

/**
 * The signature element: not a stat row, a diagram of the actual claim.
 *
 * Two on-chain roots, and between them the linkset drawn as one strand per relation
 * type with thickness proportional to its real count. Every number here is read from
 * the served snapshot — the strands ARE the 113 edges that join the two graphs, so
 * the picture can't drift from the truth it illustrates.
 */
export default function RootLedger({ stats }: { stats: Stats }) {
  const [a, b] = stats.publishers;
  if (!a || !b) return null;

  const strands = stats.linkset.slice(0, 6);
  const max = Math.max(...strands.map((s) => s.count), 1);
  const H = 34;                       // vertical spacing per strand
  const height = strands.length * H + 20;
  const W = 300;                      // viewBox width; the SVG scales to its column

  return (
    <>
      <div className="ledger">
        <div className="root root-a">
          <span className="root-role">Publisher A — the platform</span>
          <span className="root-name">audius</span>
          <span className="root-addr">{shortAddr(a.owner)}</span>
          <div className="root-stats">
            <div className="root-stat"><b>{a.total.toLocaleString()}</b><span>records</span></div>
            <div className="root-stat"><b>{(a.counts.Track ?? 0).toLocaleString()}</b><span>tracks</span></div>
            <div className="root-stat"><b>{(a.counts.Artist ?? 0).toLocaleString()}</b><span>artists</span></div>
          </div>
        </div>

        <div className="strands" aria-hidden="true">
          <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" role="presentation">
            {strands.map((s, i) => {
              const y = 14 + i * H;
              // A gentle S-curve reads as a join rather than a wire.
              const d = `M0 ${height / 2} C ${W * 0.42} ${height / 2}, ${W * 0.58} ${y}, ${W} ${y}`;
              const w = 1.5 + (s.count / max) * 5;
              const len = W * 1.35;
              return (
                <g key={s.rel}>
                  <path
                    className="strand-path"
                    d={d}
                    stroke={i === 0 ? 'var(--primary)' : 'var(--secondary)'}
                    strokeWidth={w}
                    opacity={0.35 + 0.5 * (s.count / max)}
                    style={{ ['--len' as string]: len, animationDelay: `${i * 70}ms` }}
                  />
                  <text className="strand-label" x={W * 0.52} y={y - 7} textAnchor="middle"
                        style={{ animationDelay: `${i * 70 + 550}ms` }}>
                    {s.count} {s.rel}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>

        <div className="root root-b">
          <span className="root-role">Publisher B — the artist</span>
          {b.labelId ? (
            <button className="root-name" style={{ padding: 0, textAlign: 'left' }}
                    onClick={() => goEntity(b.labelId!)}>
              {b.label}
            </button>
          ) : (
            <span className="root-name">{b.label ?? 'the artist'}</span>
          )}
          <span className="root-addr">{shortAddr(b.owner)}</span>
          <div className="root-stats">
            <div className="root-stat"><b>{b.total.toLocaleString()}</b><span>records</span></div>
            <div className="root-stat"><b>{(b.counts.Track ?? 0).toLocaleString()}</b><span>tracks</span></div>
            <div className="root-stat"><b>{(b.counts.Playlist ?? 0).toLocaleString()}</b><span>releases</span></div>
          </div>
        </div>
      </div>

      <p className="ledger-caption">
        <b>{stats.linksetTotal}</b> edges cross between the two roots. Neither publisher
        can assert them alone, and neither holds the other's catalogue.
      </p>
    </>
  );
}
