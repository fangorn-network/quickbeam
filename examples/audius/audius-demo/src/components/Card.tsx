import { useState } from 'react';
import { duration, fallbackArt, initial, nfmt, recArt } from '../lib/format';
import { isPlayable } from '../lib/player';
import { goEntity } from '../lib/router';
import type { Rec } from '../lib/types';
import AddButton from './AddButton';
import Chip from './Chip';
import PlayButton from './PlayButton';
import Taste from './Taste';

/** `queue` is the list this card was rendered from, so next/prev work in place. */
export default function Card({ rec, queue }: { rec: Rec; queue?: Rec[] }) {
  const f = rec.fields;
  // Content nodes 404 older CIDs. Swap to generated art on error rather than
  // hiding the <img> and leaving an empty square.
  const [broken, setBroken] = useState(false);
  const art = broken ? null : recArt(rec, 480);
  const sub =
    rec.entityType === 'Track' || rec.entityType === 'Playlist'
      ? f.artist
      : rec.entityType === 'Artist'
        ? f.handle && `@${f.handle}`
        : undefined;

  const meta =
    rec.entityType === 'Track' ? [f.genre, duration(f.duration), f.playCount ? `${nfmt(f.playCount)} plays` : '']
      : rec.entityType === 'Artist' ? [f.followerCount ? `${nfmt(f.followerCount)} followers` : '', f.trackCount ? `${f.trackCount} tracks` : '']
        : rec.entityType === 'Playlist' ? [f.kind, f.trackCount ? `${f.trackCount} tracks` : '']
          : [rec.entityType];

  return (
    <button className="card" onClick={() => goEntity(rec.id)}>
      <div className="card-art">
        {art ? (
          <img src={art} alt="" loading="lazy" onError={() => setBroken(true)} />
        ) : (
          <div className="art-fallback" style={{ background: fallbackArt(rec.id) }}>
            {initial(f.title)}
          </div>
        )}
        <span className="card-chip"><Chip owner={rec.owner} /></span>
        <span className="card-add"><AddButton rec={rec} size="sm" /></span>
        {isPlayable(rec) && (
          <span className="card-play"><PlayButton rec={rec} queue={queue} /></span>
        )}
        <span className="card-taste"><Taste rec={rec} size="sm" /></span>
      </div>
      <div className="card-body">
        <span className="card-title">{f.title || rec.id}</span>
        {sub && <span className="card-sub">{sub}</span>}
        <span className="card-meta">{meta.filter(Boolean).join(' · ')}</span>
      </div>
    </button>
  );
}
