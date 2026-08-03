import { useEffect, useState } from 'react';
import Chip from '../components/Chip';
import PlayButton from '../components/PlayButton';
import Rail from '../components/Rail';
import Taste from '../components/Taste';
import { entity, neighbours, relations } from '../lib/client';
import { isPlayable, usePlayer } from '../lib/player';
import {
  duration, fallbackArt, initial, nfmt, recArt, shortAddr, splitTags,
} from '../lib/format';
import { goSearch } from '../lib/router';
import type { Rec, RelationGroup } from '../lib/types';

export default function Entity({ id }: { id: string }) {
  const [rec, setRec] = useState<Rec | null | undefined>(undefined);
  const [rels, setRels] = useState<RelationGroup[]>([]);
  const [broken, setBroken] = useState(false);
  const { play } = usePlayer();

  useEffect(() => {
    let live = true;
    setRec(undefined);
    setRels([]);
    setBroken(false);
    void entity(id).then((r) => { if (live) setRec(r); });
    void relations(id).then((r) => { if (live) setRels(r); });
    return () => { live = false; };
  }, [id]);

  if (rec === undefined) {
    return <div className="loading"><div className="spinner" /><span>Loading record…</span></div>;
  }
  if (rec === null) {
    return (
      <div className="empty">
        <h3>Not in this snapshot</h3>
        <p>That record isn't part of either published graph.</p>
      </div>
    );
  }

  const f = rec.fields;
  const isArtist = rec.entityType === 'Artist';
  const art = broken ? null : recArt(rec, 1000);
  const tags = splitTags(f.tags);

  return (
    <>
      <button className="back" onClick={() => history.back()}>← Back</button>

      <header className="entity-head">
        <div className={`entity-art${isArtist ? ' round' : ''}`}>
          {art ? (
            <img src={art} alt="" onError={() => setBroken(true)} />
          ) : (
            <div className="art-fallback" style={{ background: fallbackArt(rec.id), fontSize: 56 }}>
              {initial(f.title)}
            </div>
          )}
        </div>

        <div className="entity-meta">
          <span className="entity-type">
            {f.kind ?? rec.entityType}
            {f.isReference ? ' · platform reference' : ''}
          </span>
          <h1>{f.title || rec.id}</h1>
          {f.artist && !isArtist && <span className="entity-sub">{f.artist}</span>}
          {isArtist && f.handle && <span className="entity-sub">@{f.handle}</span>}

          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            {isPlayable(rec) && <PlayButton rec={rec} size="lg" />}
            <Taste rec={rec} />
            {/* Seeds the queue from whichever relation actually holds this page's
                tracks — `contains` for a playlist, `created` for an artist. */}
            {(rec.entityType === 'Playlist' || rec.entityType === 'Artist') && (
              <PlayAll id={id} rel={rec.entityType === 'Playlist' ? 'contains' : 'created'}
                       onPlay={play} />
            )}
            <Chip owner={rec.owner} showAddr />
            {f.isVerified ? <span className="tag">Verified on Audius</span> : null}
          </div>

          <div className="entity-stats">
            {f.playCount ? <div><b>{nfmt(f.playCount)}</b><span>plays</span></div> : null}
            {f.followerCount ? <div><b>{nfmt(f.followerCount)}</b><span>followers</span></div> : null}
            {f.trackCount ? <div><b>{nfmt(f.trackCount)}</b><span>tracks</span></div> : null}
            {f.favoriteCount ? <div><b>{nfmt(f.favoriteCount)}</b><span>favorites</span></div> : null}
            {f.repostCount ? <div><b>{nfmt(f.repostCount)}</b><span>reposts</span></div> : null}
            {f.duration ? <div><b>{duration(f.duration)}</b><span>length</span></div> : null}
            {f.releaseDate ? <div><b>{f.releaseDate}</b><span>released</span></div> : null}
          </div>
        </div>
      </header>

      {/* The sovereignty moment, stated where it happens rather than in a footnote. */}
      {f.isReference && (
        <div className="banner">
          <div>
            <b>This is the platform's reference, not the artist's record.</b> It carries an
            id and a handle and nothing else — no profile, no catalogue, no artwork,
            because the platform doesn't hold them. Follow <em>Same artist as</em> below
            to reach the record the artist published from their own wallet
            {rec.owner ? <> (<code>{shortAddr(rec.owner)}</code> → a different key)</> : null}.
          </div>
        </div>
      )}

      {(f.bio || f.description) && <p className="prose">{f.bio || f.description}</p>}

      {(f.genre || f.mood || tags.length > 0) && (
        <div className="tags">
          {f.genre && <button className="tag" onClick={() => goSearch(String(f.genre))}>{f.genre}</button>}
          {f.mood && <button className="tag" onClick={() => goSearch(String(f.mood))}>{f.mood}</button>}
          {tags.map((t) => (
            <button key={t} className="tag" onClick={() => goSearch(t)}>#{t}</button>
          ))}
        </div>
      )}

      {rels.map((g) => <Rail key={`${g.dir}:${g.rel}`} group={g} id={id} />)}

      {rels.length === 0 && (
        <div className="empty"><p>No relations recorded for this node.</p></div>
      )}
    </>
  );
}

/**
 * "Play all" for a page whose tracks live behind a relation. Resolves the rail once
 * on click rather than on render, so opening a page costs nothing extra.
 */
function PlayAll({ id, rel, onPlay }: {
  id: string; rel: string; onPlay: (rec: Rec, queue?: Rec[]) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [empty, setEmpty] = useState(false);
  if (empty) return null;
  return (
    <button
      className="playall"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          const { records } = await neighbours(id, rel, 'out', 100);
          const tracks = records.filter(isPlayable);
          if (tracks.length) onPlay(tracks[0], tracks);
          else setEmpty(true);
        } finally { setBusy(false); }
      }}
    >
      {busy ? 'Loading…' : 'Play all'}
    </button>
  );
}
