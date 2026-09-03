// Your playlists — index and one editor, behind a single route.
//
// Nothing here is on-chain and nothing is uploaded. A playlist is a name and a list
// of record ids; the records themselves are already in the snapshot this tab
// downloaded, so there is nothing to store and nothing to keep in sync.
import { useEffect, useMemo, useRef, useState } from 'react';
import PlayButton from '../components/PlayButton';
import Taste from '../components/Taste';
import { entities } from '../lib/client';
import { duration, fallbackArt, hms, initial, recArt } from '../lib/format';
import { isPlayable, usePlayer } from '../lib/player';
import {
  readShare, serialize, shareUrl, usePlaylists, type Playlist,
} from '../lib/playlists.tsx';
import { goEntity, goPlaylists } from '../lib/router';
import type { Rec } from '../lib/types';

/**
 * Resolve saved ids to records in ONE round trip, as a lookup keyed by id.
 *
 * Keyed rather than positional on purpose. The worker answers index-aligned with the
 * ids it was given, but the playlist can change while that answer is in flight —
 * remove a track and a positional array is silently off by one, showing the wrong
 * title against every row below it. A map cannot go out of step with a list.
 *
 * It also means the SET of ids is the dependency, not their order, so reordering
 * never refetches. Three states, all distinct and all needed: null is "not loaded",
 * a missing key is "not fetched yet" (a track added a moment ago), and a present
 * null is the real answer — this snapshot does not carry that id.
 *
 * That last case is reported, never silently pruned: a worker error or a wrong
 * VITE_CDN_URL would otherwise quietly delete somebody's playlist.
 */
function useRecords(ids: string[]): Map<string, Rec | null> | null {
  const [map, setMap] = useState<Map<string, Rec | null> | null>(null);
  // Serialized rather than joined: record ids carry ':' and '/', and picking a
  // separator is picking a character they will one day contain.
  const key = JSON.stringify([...new Set(ids)].sort());
  useEffect(() => {
    const list = JSON.parse(key) as string[];
    if (!list.length) { setMap(new Map()); return; }
    let live = true;
    void entities(list)
      .then((recs) => {
        if (live) setMap(new Map(list.map((id, i) => [id, recs[i] ?? null])));
      })
      .catch(() => { if (live) setMap(new Map()); });
    return () => { live = false; };
  }, [key]);
  return map;
}

/**
 * One row's contents: art, title, duration, controls — or the placeholder when the
 * record isn't there. Shared by the editor and the read-only shared-link preview.
 *
 * Deliberately does NOT own the row's <li>, its number, grip or move buttons: those
 * are the editable parts, and keeping them outside is what lets the preview reuse
 * this without a `readOnly` flag threaded through the editor.
 */
function TrackCell(
  { rec, tid, known, queue }: { rec: Rec | null; tid: string; known: boolean; queue: Rec[] },
) {
  if (!rec) {
    return (
      <span className="pl-title pl-title-dead">
        <span className="pl-title-main">{known ? 'Not in this snapshot' : 'Loading…'}</span>
        {known && <span className="pl-title-sub">{tid}</span>}
      </span>
    );
  }
  const art = recArt(rec, 150);
  return (
    <>
      <span className="pl-art">
        {art
          ? <img src={art} alt="" loading="lazy" />
          : <span className="art-fallback" style={{ background: fallbackArt(rec.id) }} />}
      </span>
      <button className="pl-title" onClick={() => goEntity(rec.id)}>
        <span className="pl-title-main">{rec.fields.title || rec.id}</span>
        <span className="pl-title-sub">{rec.fields.artist}</span>
      </button>
      <span className="pl-dur">{duration(rec.fields.duration)}</span>
      <span className="pl-row-controls">
        <PlayButton rec={rec} queue={queue} />
        <Taste rec={rec} size="sm" />
      </span>
    </>
  );
}

/** Up to four covers, 2x2. One cover fills the square; none falls back to the same
 *  generated art a record with no artwork gets. */
function Cover({ pl, recs }: { pl: Playlist; recs: (Rec | null)[] }) {
  const arts = recs
    .flatMap((r) => (r ? [recArt(r, 150)] : []))
    .filter((a): a is string => !!a);
  if (!arts.length) {
    return (
      <div className="art-fallback" style={{ background: fallbackArt(pl.id) }}>
        {initial(pl.name)}
      </div>
    );
  }
  const shown = arts.length >= 4 ? arts.slice(0, 4) : arts.slice(0, 1);
  return (
    <div className={`pl-cover${shown.length === 4 ? ' is-mosaic' : ''}`}>
      {shown.map((a) => <img key={a} src={a} alt="" loading="lazy" />)}
    </div>
  );
}

const totalSecs = (recs: (Rec | null)[]) =>
  recs.reduce((n, r) => n + (r ? Number(r.fields.duration) || 0 : 0), 0);

function ImportButton() {
  const { importJson } = usePlaylists();
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <>
      {/* A label wrapping the input is a keyboard-operable button with no JS. */}
      <label className="playall pl-import">
        Import
        <input
          type="file"
          accept="application/json,.json"
          onChange={async (e) => {
            const file = e.target.files?.[0];
            // Reset first, or picking the same file twice fires no change event.
            e.target.value = '';
            if (!file) return;
            const { playlists, tracks } = importJson(await file.text());
            setMsg(!playlists && !tracks
              ? 'Nothing new in that file.'
              : `Added ${playlists} playlist${playlists === 1 ? '' : 's'} and ${tracks} track${tracks === 1 ? '' : 's'}.`);
          }}
        />
      </label>
      {msg && <span className="pl-note" role="status">{msg}</span>}
    </>
  );
}

function ExportButton({ what, name }: { what: Playlist[]; name: string }) {
  if (!what.length) return null;
  return (
    <button
      className="playall"
      onClick={() => {
        const url = URL.createObjectURL(
          new Blob([serialize(what)], { type: 'application/json' }),
        );
        const a = document.createElement('a');
        a.href = url;
        a.download = `${name}-${new Date().toISOString().slice(0, 10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
      }}
    >
      Export
    </button>
  );
}

/**
 * Copy a link that CARRIES these playlists — see playlists.ts's sharing section. There
 * is no server involved and nothing is uploaded: the payload is the link.
 *
 * The fallback is not decoration. navigator.clipboard is undefined outside a secure
 * context, which includes the bare LAN IP the README's "showing it on another device"
 * section tells you to use — the same trap playlists.tsx works around for
 * crypto.randomUUID. When it's missing the URL is shown in a readonly input instead,
 * selected and ready for ctrl-C, which needs no JS and no deprecated execCommand.
 */
function ShareButton({ what, label = 'Copy link' }: { what: Playlist[]; label?: string }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const field = useRef<HTMLInputElement | null>(null);

  useEffect(() => { field.current?.select(); }, [url]);
  if (!what.length) return null;

  return (
    <>
      <button
        className="playall"
        onClick={() => {
          const href = shareUrl(what);
          const show = () => { setMsg(null); setUrl(href); };
          if (!navigator.clipboard) { show(); return; }
          void navigator.clipboard.writeText(href).then(
            () => { setUrl(null); setMsg('Link copied — it carries the playlist itself.'); },
            show,
          );
        }}
      >
        {label}
      </button>
      {msg && <span className="pl-note" role="status">{msg}</span>}
      {url && (
        <input
          className="pl-share-url"
          ref={field}
          readOnly
          value={url}
          aria-label="Share link — copy this"
          onFocus={(e) => e.target.select()}
        />
      )}
    </>
  );
}

// index ─────────────────────────────────────────────────────────────────────

function Index() {
  const { playlists, warn } = usePlaylists();
  // Every id across every playlist, deduped — one call resolves the whole page,
  // covers and running times together.
  const ids = useMemo(
    () => [...new Set(playlists.flatMap((p) => p.trackIds))],
    [playlists],
  );
  const byId = useRecords(ids);

  const sorted = useMemo(
    () => [...playlists].sort((a, b) => b.createdAt - a.createdAt),
    [playlists],
  );

  return (
    <section>
      <div className="section-head">
        <h2>Your playlists <span className="count">{playlists.length || ''}</span></h2>
        <div className="pl-actions">
          <ExportButton what={playlists} name="audius-playlists" />
          <ShareButton what={playlists} label="Copy link to all" />
          <ImportButton />
        </div>
      </div>

      {warn && <p className="dlg-warn">{warn}</p>}

      {!playlists.length ? (
        <div className="empty">
          <h3>No playlists yet</h3>
          <p>
            Press <b>+</b> on any track to start one. They are saved in this browser
            only — never uploaded, and never attached to an account.
          </p>
        </div>
      ) : (
        <div className="grid">
          {sorted.map((pl) => {
            const recs = pl.trackIds.map((t) => byId?.get(t) ?? null);
            return (
              <button key={pl.id} className="card" onClick={() => goPlaylists(pl.id)}>
                <div className="card-art"><Cover pl={pl} recs={recs} /></div>
                <div className="card-body">
                  <span className="card-title">{pl.name}</span>
                  <span className="card-meta">
                    {pl.trackIds.length} track{pl.trackIds.length === 1 ? '' : 's'}
                    {byId ? ` · ${hms(totalSecs(recs))}` : ''}
                  </span>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}

// one playlist ──────────────────────────────────────────────────────────────

function Detail({ id }: { id: string }) {
  const { playlists, warn, rename, remove, removeTrack, reorder } = usePlaylists();
  const { play } = usePlayer();
  const pl = playlists.find((p) => p.id === id);

  const byId = useRecords(pl?.trackIds ?? []);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  // Reordering is invisible to a screen reader without this.
  const [live, setLive] = useState('');
  const nameRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => { if (editing) nameRef.current?.select(); }, [editing]);

  if (!pl) {
    return (
      <div className="empty">
        <h3>No such playlist</h3>
        <button className="back" onClick={() => goPlaylists()}>Your playlists</button>
      </div>
    );
  }

  const recs = pl.trackIds.map((t) => byId?.get(t) ?? null);
  const playable = recs.filter((r): r is Rec => !!r && isPlayable(r));
  // Only ids the worker actually answered for — a track added a moment ago is
  // absent from the map, and calling that "missing" would be a lie for one frame.
  const missing = byId ? pl.trackIds.filter((t) => byId.has(t) && !byId.get(t)).length : 0;

  const move = (from: number, to: number) => {
    if (to < 0 || to >= pl.trackIds.length) return;
    reorder(pl.id, from, to);
    const title = recs[from]?.fields.title ?? 'Track';
    setLive(`Moved ${title} to position ${to + 1} of ${pl.trackIds.length}`);
  };

  return (
    <section>
      <button className="back" onClick={() => goPlaylists()}>Your playlists</button>

      <div className="pl-head">
        <div className="pl-head-art"><Cover pl={pl} recs={recs} /></div>
        <div className="pl-head-body">
          {editing ? (
            <form
              className="pl-rename"
              onSubmit={(e) => { e.preventDefault(); rename(pl.id, draft); setEditing(false); }}
            >
              <input
                ref={nameRef}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={() => { rename(pl.id, draft); setEditing(false); }}
                aria-label="Playlist name"
                maxLength={80}
              />
            </form>
          ) : (
            // Stays an <h1> when idle: a permanent input where the heading belongs
            // wrecks the document outline for anyone navigating by headings.
            <h1>{pl.name}</h1>
          )}
          <p className="pl-stats">
            {pl.trackIds.length} track{pl.trackIds.length === 1 ? '' : 's'}
            {byId && ` · ${hms(totalSecs(recs))}`}
          </p>
          <div className="pl-actions">
            {!!playable.length && (
              <button className="playall" onClick={() => play(playable[0], playable)}>
                Play all
              </button>
            )}
            {!editing && (
              <button
                className="playall"
                onClick={() => { setDraft(pl.name); setEditing(true); }}
              >
                Rename
              </button>
            )}
            <ExportButton what={[pl]} name={pl.name.replace(/[^\w-]+/g, '-').toLowerCase() || 'playlist'} />
            <ShareButton what={[pl]} />
            <button
              className="playall pl-danger"
              onClick={() => {
                // Irreversible, so it asks. confirm() is one line and accessible.
                if (confirm(`Delete "${pl.name}"? This cannot be undone.`)) {
                  remove(pl.id);
                  goPlaylists();
                }
              }}
            >
              Delete
            </button>
          </div>
        </div>
      </div>

      {warn && <p className="dlg-warn">{warn}</p>}

      {missing > 0 && (
        <p className="pl-note">
          {missing} track{missing === 1 ? ' is' : 's are'} not in this snapshot.{' '}
          <button
            className="pl-linkbtn"
            onClick={() => pl.trackIds
              .filter((t) => byId?.has(t) && !byId.get(t))
              .forEach((t) => removeTrack(pl.id, t))}
          >
            Remove {missing === 1 ? 'it' : 'them'}
          </button>
        </p>
      )}

      <span className="sr-only" aria-live="polite">{live}</span>

      {!byId ? (
        <div className="loading"><div className="spinner" /><span>Loading tracks…</span></div>
      ) : !pl.trackIds.length ? (
        <div className="empty">
          <h3>Nothing in here yet</h3>
          <p>Press <b>+</b> on any track to add it.</p>
        </div>
      ) : (
        <ol className="pl-rows">
          {pl.trackIds.map((tid, i) => {
            const rec = recs[i];
            const known = byId.has(tid);
            return (
              <li
                // Keyed by track, not index: the browser keeps focus on a moved node,
                // so the arrow button you just pressed stays focused across the move.
                key={tid}
                // Dimmed only once the worker has actually said "no such id" — a row
                // still in flight must not look like a broken one.
                className={`pl-row${over === i ? ' is-over' : ''}${known && !rec ? ' is-dead' : ''}`}
                aria-busy={!known || undefined}
                draggable
                onDragStart={(e) => {
                  setDragFrom(i);
                  e.dataTransfer.effectAllowed = 'move';
                  // Firefox will not start a drag unless data is set.
                  e.dataTransfer.setData('text/plain', String(i));
                }}
                // Without preventDefault, drop never fires. The classic native-DnD bug.
                onDragOver={(e) => { e.preventDefault(); setOver(i); }}
                onDrop={(e) => {
                  e.preventDefault();
                  if (dragFrom !== null) move(dragFrom, i);
                  setDragFrom(null); setOver(null);
                }}
                onDragEnd={() => { setDragFrom(null); setOver(null); }}
              >
                <span className="pl-grip" aria-hidden="true">::</span>
                <span className="pl-num">{i + 1}</span>

                <TrackCell rec={rec} tid={tid} known={known} queue={playable} />

                {/* Always visible, never hover-revealed: HTML5 drag does not fire on
                    touch at all, so on a phone these are the ONLY way to reorder. */}
                <span className="pl-move">
                  <button
                    aria-label={`Move ${rec?.fields.title ?? 'track'} up`}
                    disabled={i === 0}
                    onClick={() => move(i, i - 1)}
                  >
                    &uarr;
                  </button>
                  <button
                    aria-label={`Move ${rec?.fields.title ?? 'track'} down`}
                    disabled={i === pl.trackIds.length - 1}
                    onClick={() => move(i, i + 1)}
                  >
                    &darr;
                  </button>
                  <button
                    aria-label={`Remove ${rec?.fields.title ?? 'track'} from ${pl.name}`}
                    onClick={() => removeTrack(pl.id, tid)}
                  >
                    &times;
                  </button>
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

// a shared link ─────────────────────────────────────────────────────────────

/**
 * Someone else's playlists, read out of the URL. NOT saved — that is the whole point
 * of previewing rather than importing on arrival: a link should not be able to write
 * to your library just because you clicked it. Play it, then decide.
 *
 * Everything here is read-only: no rename, reorder, remove or delete, and the
 * "not in this snapshot" note appears WITHOUT the editor's remove button, since
 * pruning someone else's playlist before you have even taken it makes no sense.
 */
function Shared({ payload }: { payload: string }) {
  const { importJson } = usePlaylists();
  const { play } = usePlayer();
  const [saved, setSaved] = useState<string | null>(null);

  // Decoding is pure and the payload only changes with the URL, so this runs once.
  const pls = useMemo(() => readShare(payload), [payload]);
  const ids = useMemo(() => [...new Set(pls.flatMap((p) => p.trackIds))], [pls]);
  const byId = useRecords(ids);

  if (!pls.length) {
    return (
      <div className="empty">
        <h3>That link didn't carry a playlist</h3>
        <p>It may have been truncated on the way here — links are cut short by some
          chat apps. Ask for it again, or for the exported file instead.</p>
        <button className="back" onClick={() => goPlaylists()}>Your playlists</button>
      </div>
    );
  }

  const recsOf = (pl: Playlist) => pl.trackIds.map((t) => byId?.get(t) ?? null);
  const one = pls.length === 1 ? pls[0] : null;
  const recs = one ? recsOf(one) : [];
  const playable = recs.filter((r): r is Rec => !!r && isPlayable(r));
  // Over `ids`, which is already deduped — a track in two of the shared playlists is
  // one missing track, not two.
  const missing = byId ? ids.filter((t) => byId.has(t) && !byId.get(t)).length : 0;
  const tracks = pls.reduce((n, p) => n + p.trackIds.length, 0);

  return (
    <section>
      <button className="back" onClick={() => goPlaylists()}>Your playlists</button>

      <div className="pl-head">
        <div className="pl-head-art">
          <Cover pl={pls[0]} recs={one ? recs : recsOf(pls[0])} />
        </div>
        <div className="pl-head-body">
          <h1>{one ? one.name : `${pls.length} playlists`}</h1>
          <p className="pl-stats">
            Shared with you · {tracks} track{tracks === 1 ? '' : 's'}
            {one && byId ? ` · ${hms(totalSecs(recs))}` : ''}
          </p>
          <div className="pl-actions">
            {!!playable.length && (
              <button className="playall" onClick={() => play(playable[0], playable)}>
                Play all
              </button>
            )}
            <button
              className="playall"
              onClick={() => {
                // The same merge an imported file goes through, so this can only ever
                // ADD — it never renames or deletes what you already have, and
                // pressing it twice does nothing the second time.
                const n = importJson(serialize(pls));
                setSaved(!n.playlists && !n.tracks
                  ? 'Already in your playlists.'
                  : `Saved ${n.playlists} playlist${n.playlists === 1 ? '' : 's'} and ${n.tracks} track${n.tracks === 1 ? '' : 's'}.`);
              }}
            >
              Save to your playlists
            </button>
            {saved && <span className="pl-note" role="status">{saved}</span>}
          </div>
        </div>
      </div>

      <p className="pl-note">
        Nothing is saved to this browser until you press Save.
      </p>

      {missing > 0 && (
        <p className="pl-note">
          {missing} track{missing === 1 ? ' is' : 's are'} not in this snapshot and
          won't play.
        </p>
      )}

      {one ? (
        <ol className="pl-rows">
          {one.trackIds.map((tid, i) => (
            <li key={tid} className={`pl-row${byId?.has(tid) && !byId.get(tid) ? ' is-dead' : ''}`}>
              <span className="pl-num">{i + 1}</span>
              <TrackCell
                rec={recs[i]}
                tid={tid}
                known={!!byId?.has(tid)}
                queue={playable}
              />
            </li>
          ))}
        </ol>
      ) : (
        <div className="grid">
          {pls.map((pl) => (
            // Not a link: these aren't addressable until they're saved.
            <div key={pl.id} className="card">
              <div className="card-art"><Cover pl={pl} recs={recsOf(pl)} /></div>
              <div className="card-body">
                <span className="card-title">{pl.name}</span>
                <span className="card-meta">
                  {pl.trackIds.length} track{pl.trackIds.length === 1 ? '' : 's'}
                  {byId ? ` · ${hms(totalSecs(recsOf(pl)))}` : ''}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default function Playlists({ id, share }: { id: string; share?: string }) {
  // A share payload outranks an id: it addresses a playlist that is not in this
  // browser yet, so there would be nothing for `id` to find.
  if (share) return <Shared payload={share} />;
  return id ? <Detail id={id} /> : <Index />;
}
