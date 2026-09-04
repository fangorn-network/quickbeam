import { useEffect, useState } from 'react';
import AudiusBadge from '../components/AudiusBadge';
import PlayButton from '../components/PlayButton';
import Rail, { RecordRail } from '../components/Rail';
import Taste from '../components/Taste';
import { discovery, entity, neighbours, relations } from '../lib/client';
import { isPlayable, usePlayer } from '../lib/player';
import {
  duration, fallbackArt, initial, nfmt, recArt, splitTags,
} from '../lib/format';
import { goSearch } from '../lib/router';
import type { Rec, RelationGroup } from '../lib/types';

/** The relation that holds a page's own contents, by entity type. */
const PRIMARY_REL: Record<string, string | undefined> = {
  Artist: 'created',
  Playlist: 'contains',
};

/**
 * Reading order for a TRACK's rails: who made it, where it lives, then what it
 * is, narrowing from attribution to classification.
 *
 * This deliberately overrides `Graph.relations`, whose ranking is right for
 * browsing a graph and wrong for reading a track.
 *
 * Anything unlisted keeps its incoming order and sits between mood and tags,
 * since tags are the weakest discovery signal and were asked for last.
 */
const TRACK_RAIL_ORDER = ['in:created', 'in:contains', 'out:inGenre', 'out:hasMood'];
const TRACK_RAIL_LAST = 'out:taggedWith';

/**
 * The rails that describe what a track IS, rather than where it came from.
 *
 * Discovery is injected immediately before the first of these — the point where
 * the page stops describing this record and starts pointing at others. Defined
 * as a set rather than an index so it still lands correctly for the ~10% of
 * tracks that appear in no playlist and therefore have no "Appears in" rail.
 */
const CLASSIFICATION_RAILS = new Set(['out:inGenre', 'out:hasMood', 'out:taggedWith']);

function orderRails(groups: RelationGroup[], entityType: string): RelationGroup[] {
  if (entityType !== 'Track') return groups;
  const rank = (g: RelationGroup) => {
    const key = `${g.dir}:${g.rel}`;
    if (key === TRACK_RAIL_LAST) return TRACK_RAIL_ORDER.length + 1;
    const i = TRACK_RAIL_ORDER.indexOf(key);
    return i === -1 ? TRACK_RAIL_ORDER.length : i;
  };
  // Array.sort is stable, so unlisted relations keep the order Graph gave them.
  return [...groups].sort((a, b) => rank(a) - rank(b));
}

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
        <p>That record isn't in this catalogue.</p>
      </div>
    );
  }

  const f = rec.fields;
  const isArtist = rec.entityType === 'Artist';
  const isTrack = rec.entityType === 'Track';
  // Which relation holds this page's own contents — the same mapping "Play all"
  // already uses, kept in one place so the two cannot disagree about it.
  const primaryRel = PRIMARY_REL[rec.entityType];

  // `trackCount` is Audius' own figure for the artist. It is NOT what this graph
  // holds, and for 94% of artists here the two differ — often wildly (one account
  // reports 5,343 against the 5 we carry). The crawl is bounded on purpose:
  // `crawl.py` expands only --max-artists profiles at --per-artist-tracks each,
  // because it talks to volunteer-run public infrastructure. Only the focus
  // artist is fetched complete.
  //
  // So show what we actually have, and show Audius' number beside it rather than
  // instead of it — a page claiming 103 tracks above a rail of 9 reads as a bug.
  const heldTracks = rels.find((r) => r.rel === 'created' && r.dir === 'out')?.count ?? 0;
  const audiusTracks = Number(f.trackCount ?? 0);
  const partialCatalogue = isArtist && heldTracks > 0 && audiusTracks > heldTracks;

  const ordered = orderRails(rels, rec.entityType);
  const split = isTrack
    ? (() => {
        const i = ordered.findIndex((g) => CLASSIFICATION_RAILS.has(`${g.dir}:${g.rel}`));
        return i === -1 ? ordered.length : i;
      })()
    : ordered.length;
  const beforeDiscovery = ordered.slice(0, split);
  const afterDiscovery = ordered.slice(split);

  const renderRail = (g: RelationGroup) => (
    <Rail
      key={`${id}:${g.dir}:${g.rel}`}
      group={g}
      id={id}
      limit={g.rel === primaryRel && g.dir === 'out' ? 60 : 12}
      note={partialCatalogue && g.rel === 'created' && g.dir === 'out'
        ? `${heldTracks} of their ${nfmt(audiusTracks)} Audius tracks are in the catalogue`
        : undefined}
    />
  );
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
          </span>
          <h1>{f.title || rec.id}</h1>
          {f.artist && !isArtist && <span className="entity-sub">{f.artist}</span>}
          {isArtist && f.handle && <span className="entity-sub">@{f.handle}</span>}

          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            {isPlayable(rec) && <PlayButton rec={rec} size="lg" />}
            <Taste rec={rec} />
            {/* Seeds the queue from whichever relation actually holds this page's
                tracks — `contains` for a playlist, `created` for an artist. */}
            {primaryRel && <PlayAll id={id} rel={primaryRel} onPlay={play} />}
            <AudiusBadge />
            {f.isVerified ? <span className="tag">Verified on Audius</span> : null}
          </div>

          <div className="entity-stats">
            {f.playCount ? <div><b>{nfmt(f.playCount)}</b><span>plays</span></div> : null}
            {f.followerCount ? <div><b>{nfmt(f.followerCount)}</b><span>followers</span></div> : null}
            {isArtist && heldTracks > 0 && (
              <div><b>{nfmt(heldTracks)}</b><span>tracks here</span></div>
            )}
            {partialCatalogue && (
              <div title="The catalogue holds a slice of Audius, not all of it.">
                <b>{nfmt(audiusTracks)}</b><span>on Audius</span>
              </div>
            )}
            {!isArtist && f.trackCount ? (
              <div><b>{nfmt(f.trackCount)}</b><span>tracks</span></div>
            ) : null}
            {f.favoriteCount ? <div><b>{nfmt(f.favoriteCount)}</b><span>favorites</span></div> : null}
            {f.repostCount ? <div><b>{nfmt(f.repostCount)}</b><span>reposts</span></div> : null}
            {f.duration ? <div><b>{duration(f.duration)}</b><span>length</span></div> : null}
            {f.releaseDate ? <div><b>{f.releaseDate}</b><span>released</span></div> : null}
          </div>
        </div>
      </header>

      {/* Says why this page is nearly empty, at the point someone notices. */}
      {f.isReference && (
        <div className="banner">
          <div>
            <b>This artist publishes their own catalogue.</b> The record here carries an
            id and a handle and nothing else — no profile, no tracks, no artwork.
            Follow <em>Same artist as</em> below to reach the one they publish
            themselves.
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

      {/* The page's primary relation gets a bigger window — a catalogue cut to 12
          hides the thing you opened the page for. Keyed on id as well as the
          relation so navigating between artists resets each rail's expansion. */}
      {beforeDiscovery.map(renderRail)}

      {/* Tracks: between attribution and classification. Artists: after their
          catalogue, since an artist page has no classification tail to sit in
          front of. */}
      {(isTrack || isArtist) && (
        <Discovery id={id} kind={isTrack ? 'Track' : 'Artist'} />
      )}

      {afterDiscovery.map(renderRail)}

      {rels.length === 0 && (
        <div className="empty"><p>No relations recorded for this node.</p></div>
      )}
    </>
  );
}

/**
 * Two derived rails, for artists only.
 *
 * Nothing in the source data says two artists are related — there is no
 * `relatedTo` edge — so both of these are computed, and each is labelled with
 * how, because "related" is a claim and the page should say who is making it.
 *
 * They are separate rails rather than one blended list on purpose: one walks
 * real playlist edges and one measures embedding distance. Those are the mesh's
 * two axes, and merging them would hide which answer came from where.
 */
const DISCOVERY_LABELS = {
  Artist: {
    peers: { title: 'Often in playlists with', note: 'curators put them side by side' },
    similar: { title: 'Sounds similar', note: 'nearest to this catalogue in embedding space' },
  },
  Track: {
    peers: { title: 'Often heard alongside', note: 'shares playlists with this track' },
    similar: { title: 'You may also like', note: 'nearest to this track in embedding space' },
  },
} as const;

function Discovery({ id, kind }: { id: string; kind: 'Artist' | 'Track' }) {
  const [d, setD] = useState<{ peers: Rec[]; similar: Rec[] } | null>(null);

  useEffect(() => {
    let live = true;
    setD(null);
    void discovery(id).then((r) => { if (live) setD(r); });
    return () => { live = false; };
  }, [id]);

  if (!d) return null;

  const L = DISCOVERY_LABELS[kind];

  return (
    <>
      <RecordRail
        title={L.peers.title}
        count={d.peers.length || undefined}
        note={L.peers.note}
        recs={d.peers}
      />
      <RecordRail
        title={L.similar.title}
        count={d.similar.length || undefined}
        note={L.similar.note}
        recs={d.similar}
      />
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
