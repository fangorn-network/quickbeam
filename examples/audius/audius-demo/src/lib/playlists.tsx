// Main-thread handle on the playlist store. The logic lives in playlists.ts (pure,
// node-importable); this file owns the two things it cannot: localStorage and React.
//
// It also owns exactly ONE <dialog> for the whole app — the "add to a playlist"
// picker. Same argument player.tsx makes for having exactly one <audio>: a dialog per
// card would be hundreds of elements for one interaction. showModal() also puts it in
// the browser's top layer, which is how it clears the fixed now-playing bar without
// anyone inventing a z-index.
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from 'react';
import * as P from './playlists.ts';
import { shareHref } from './router';
import type { Rec } from './types';

export type { Playlist } from './playlists.ts';
// Re-exported so the view has one import source for everything playlist-shaped.
export { serialize } from './playlists.ts';

/** Untrusted input — see P.parse. A quota-full or private-mode browser reads as empty. */
function load(): P.Playlist[] {
  try { return P.parse(localStorage.getItem(P.PLAYLISTS_KEY)); } catch { return []; }
}

/** Returns false rather than swallowing, unlike kernel.tsx's save(): the kernel
 *  writes ~110 KB per signal, so the quota CAN fill, and a playlist edit that silently
 *  fails to persist is the one failure this feature must not have. */
function save(pls: P.Playlist[]): boolean {
  try { localStorage.setItem(P.PLAYLISTS_KEY, P.serialize(pls)); return true; }
  catch { return false; }
}

// base64url, the browser half of sharing (the shape lives in playlists.ts).
//
// Chosen over encodeURIComponent for one reason that is not size: its alphabet is
// `A-Za-z0-9-_`, and NOTHING in there is touched by URLSearchParams on the way back
// out. Percent-escapes would be decoded once by the browser and once by
// params.get(), and a plain base64 `+` would come back as a space.
//
// btoa is Latin-1 only, so the TextEncoder round trip is not optional — playlist
// names carry emoji and CJK.
const enc = (s: string) => btoa(String.fromCharCode(...new TextEncoder().encode(s)))
  .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

const dec = (s: string) => new TextDecoder().decode(
  Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0)),
);

/** A link that carries these playlists. Nothing is uploaded to produce it. */
export const shareUrl = (pls: P.Playlist[]) => shareHref(enc(P.toShare(pls)));

/** The other direction. Never throws — a mangled link reads as no playlists, which is
 *  what the preview renders an empty state for. */
export function readShare(payload: string): P.Playlist[] {
  try { return P.fromShare(dec(payload)); } catch { return []; }
}

/** crypto.randomUUID is undefined outside a secure context — which includes the bare
 *  LAN IP the README's "showing it on another device" section describes. */
const uuid = () =>
  crypto.randomUUID?.() ?? `pl-${Date.now()}-${Math.random().toString(36).slice(2)}`;

interface PlaylistCtx {
  playlists: P.Playlist[];
  /** Set when a write didn't persist, so the UI can say so instead of lying. */
  warn: string | null;
  /** Open the picker for a record. The only way in — there is one dialog. */
  addTo: (rec: Rec) => void;
  rename: (id: string, name: string) => void;
  remove: (id: string) => void;
  removeTrack: (id: string, trackId: string) => void;
  reorder: (id: string, from: number, to: number) => void;
  /** Returns what the import actually added, for the confirmation line. */
  importJson: (raw: string) => { playlists: number; tracks: number };
}

const Ctx = createContext<PlaylistCtx | null>(null);
export const usePlaylists = () => {
  const p = useContext(Ctx);
  if (!p) throw new Error('usePlaylists outside PlaylistProvider');
  return p;
};

export function PlaylistProvider({ children }: { children: React.ReactNode }) {
  const [playlists, setPlaylists] = useState<P.Playlist[]>(load);
  const [warn, setWarn] = useState<string | null>(null);
  const [pending, setPending] = useState<Rec | null>(null);
  const [newName, setNewName] = useState('');
  const dlg = useRef<HTMLDialogElement | null>(null);

  // Persist from the action, NOT from an effect on `playlists`. An effect also fires
  // on mount, so a blob that parse() couldn't salvage would be overwritten with [] —
  // destroying the evidence — before the visitor has touched anything.
  //
  // Every op in playlists.ts returns its input by reference when nothing changed,
  // which is what makes `next === playlists` a sufficient no-op test here.
  const commit = useCallback((next: P.Playlist[]) => {
    if (next === playlists) return;
    setPlaylists(next);
    setWarn(save(next) ? null : "This browser's storage is full — that change wasn't saved.");
  }, [playlists]);

  // Modal, imperatively. `open={…}` would render a NON-modal dialog: no backdrop, no
  // focus trap, no top layer.
  useEffect(() => {
    const d = dlg.current;
    if (!d) return;
    if (!pending) { if (d.open) d.close(); return; }
    // StrictMode double-invokes this, and showModal() on an open dialog throws
    // InvalidStateError.
    if (!d.open) d.showModal();
    const onClose = () => { setPending(null); setNewName(''); };
    d.addEventListener('close', onClose);
    return () => d.removeEventListener('close', onClose);
  }, [pending]);

  const value = useMemo<PlaylistCtx>(() => ({
    playlists,
    warn,
    addTo: (rec: Rec) => setPending(rec),
    rename: (id, name) => commit(P.rename(playlists, id, name)),
    remove: (id) => commit(P.remove(playlists, id)),
    removeTrack: (id, trackId) => commit(P.removeTrack(playlists, id, trackId)),
    reorder: (id, from, to) => commit(P.reorder(playlists, id, from, to)),
    importJson: (raw: string) => {
      // Same validator localStorage goes through, so a hostile file is already handled.
      const next = P.merge(playlists, P.parse(raw));
      commit(next);
      return {
        playlists: next.length - playlists.length,
        tracks: P.trackCount(next) - P.trackCount(playlists),
      };
    },
  }), [playlists, warn, commit]);

  // Newest first: the playlist you just made is the one you are adding to.
  const sorted = useMemo(
    () => [...playlists].sort((a, b) => b.createdAt - a.createdAt),
    [playlists],
  );

  return (
    <Ctx.Provider value={value}>
      {children}
      <dialog
        className="dlg"
        ref={dlg}
        aria-labelledby="dlg-title"
        // The padding lives on .dlg-body, so the dialog element itself is only ever
        // hit when the click landed on the backdrop.
        onClick={(e) => { if (e.target === dlg.current) dlg.current?.close(); }}
      >
        {pending && (
          <div className="dlg-body">
            <h2 id="dlg-title">Add to a playlist</h2>
            <p className="dlg-sub">{String(pending.fields.title ?? 'This track')}</p>

            {sorted.length > 0 && (
              <ul className="dlg-list">
                {sorted.map((pl) => {
                  const has = pl.trackIds.includes(pending.id);
                  return (
                    <li key={pl.id}>
                      {/* A toggle, and the dialog stays open — that IS the feedback,
                          so there is no toast to build and a misclick is undoable
                          where it happened. */}
                      <button
                        className={`dlg-pick${has ? ' is-on' : ''}`}
                        aria-pressed={has}
                        onClick={() => commit(has
                          ? P.removeTrack(playlists, pl.id, pending.id)
                          : P.addTrack(playlists, pl.id, pending.id))}
                      >
                        <span className="dlg-pick-name">{pl.name}</span>
                        <span className="dlg-pick-n">
                          {has ? '✓ added' : `${pl.trackIds.length} tracks`}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}

            <form
              className="dlg-new"
              onSubmit={(e) => {
                e.preventDefault();
                const id = uuid();
                // One commit, so a failed save can't leave an empty playlist behind.
                commit(P.addTrack(P.create(playlists, newName, id), id, pending.id));
                setNewName('');
              }}
            >
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="New playlist…"
                aria-label="New playlist name"
                autoFocus={playlists.length === 0}
                maxLength={80}
              />
              <button type="submit" disabled={!newName.trim()}>Create</button>
            </form>

            {warn && <p className="dlg-warn">{warn}</p>}

            {/* method="dialog" closes it with no JS at all. */}
            <form method="dialog"><button className="dlg-done">Done</button></form>
          </div>
        )}
      </dialog>
    </Ctx.Provider>
  );
}
