import { isPlatform, shortAddr } from '../lib/format';

/**
 * The publisher marker. Platform and artist differ by SHAPE (filled vs outlined with
 * a dot) as well as hue, so the split survives a colour-blind viewer and a projector
 * that mangles purples — which is exactly the room this gets demoed in.
 */
export default function Chip({ owner, showAddr = false }: { owner?: string; showAddr?: boolean }) {
  if (!owner) return null;
  const platform = isPlatform(owner);
  return (
    <span
      className={`chip ${platform ? 'chip-platform' : 'chip-artist'}`}
      title={`Published on-chain by ${owner}`}
    >
      {platform ? 'Platform' : 'Artist'}
      {showAddr && <span className="chip-addr">{shortAddr(owner)}</span>}
    </span>
  );
}
