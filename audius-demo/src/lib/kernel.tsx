// Main-thread handle on the session kernel. The kernel itself lives in the worker,
// beside the vectors; this holds only its snapshot and a version counter that tells
// the rail when to re-query.
//
// It also owns PERSISTENCE, because the worker cannot: localStorage is main-thread
// only. The worker exports/imports the state, this file stores it.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { D } from '../kernel/constants';
import {
  kernelExport, kernelImport, kernelSeed, kernelSignal, ready,
  type KernelSnapshot, type SignalKind,
} from './client';
import type { KernelPersist } from './graph';

const KEY = 'audius-demo.kernel';

interface Saved {
  /** Embedding width the vectors were written at. See `load()` for why it's checked. */
  dim: number;
  kernel: KernelPersist;
  liked: string[];
  disliked: string[];
}

/**
 * Read saved state, or null if there is nothing usable.
 *
 * Tolerant of every failure: localStorage is user-writable and survives redeploys,
 * so anything in it is untrusted input. A returning visitor whose saved state is
 * stale or corrupt gets a clean session, never a crash.
 *
 * The `dim` check is the one that matters. `mu`/`v`/`skips` are points in the
 * embedding space; if the corpus is ever re-baked at a different width they are
 * meaningless numbers of the wrong length, and the kernel would rank against
 * garbage rather than fail — the silent-wrongness mode this app already guards
 * against elsewhere.
 */
function load(): Saved | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Saved;
    const st = s?.kernel?.state;
    if (s?.dim !== D || !Array.isArray(st?.mu) || st.mu.length !== D) return null;
    // Indistinguishable from a fresh kernel (the same test kernelRecommend uses).
    // Restoring it would show a zeroed readout to someone who has done nothing.
    if (st.t === 0 && st.skips.length === 0) return null;
    return s;
  } catch {
    return null;
  }
}

function save(s: Saved) {
  // ponytail: synchronous write per signal (~110 KB at skip_window=20). Well under
  // a frame and far under the 5 MB quota; debounce only if a profile says so.
  try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* quota / private mode */ }
}

interface KernelCtx {
  snap: KernelSnapshot | null;
  /** Bumped on every signal. Drives the live readout strip. */
  version: number;
  /**
   * Bumped only on *settled* evidence — a track played to the end, an explicit
   * thumb, a seed or a reset. The recommendation rail keys off this instead of
   * `version` so that merely pressing play doesn't reshuffle the grid you are
   * still looking at (and just clicked into).
   *
   * The kernel itself takes every signal immediately either way; this governs
   * when the rail re-*renders*, not what the kernel knows.
   */
  railVersion: number;
  signal: (id: string, kind: SignalKind) => void;
  /** A track reached its natural end — the strongest evidence there is. */
  settle: () => void;
  /**
   * False until this visitor has a kernel — either restored from a previous visit
   * or seeded on the first screen. The app gates itself on this.
   */
  onboarded: boolean;
  /** Start a kernel from stated taste. Resolves once the seed has been applied. */
  seed: (genres: string[], artists: string[]) => Promise<void>;
  /** Records signalled on, restored across visits so controls show their state. */
  liked: Set<string>;
  disliked: Set<string>;
  refresh: (snap: KernelSnapshot) => void;
}

const Ctx = createContext<KernelCtx | null>(null);

/**
 * Tolerant on purpose: returns a no-op handle when no provider is mounted, so
 * PlayerProvider can report plays and skips without hard-depending on the kernel
 * being present.
 */
export function useKernel(): KernelCtx {
  return useContext(Ctx) ?? {
    snap: null, version: 0, railVersion: 0, signal: () => {}, settle: () => {},
    onboarded: true, seed: async () => {},
    liked: new Set(), disliked: new Set(), refresh: () => {},
  };
}

export function KernelProvider({ children }: { children: React.ReactNode }) {
  const [snap, setSnap] = useState<KernelSnapshot | null>(null);
  const [version, setVersion] = useState(0);
  const [railVersion, setRailVersion] = useState(0);
  const [liked, setLiked] = useState<Set<string>>(new Set());
  const [disliked, setDisliked] = useState<Set<string>>(new Set());

  // localStorage is read exactly once, here. `onboarded` is derived from it rather
  // than tracked separately, and deliberately does NOT re-read: a mid-session reset
  // should clear the kernel, not throw the visitor back to the first screen.
  const [saved] = useState(load);
  const [onboarded, setOnboarded] = useState(saved !== null);

  // Rehydrate a returning visitor, once.
  //
  // Gated on ready() rather than running at mount: kernelImport itself is safe
  // before the shards land, but the rail re-queries on the version bump, and
  // kernelRecommend scans a vector block that is still empty until load()
  // resolves — so an early hydrate would restore the taste and then show an
  // empty rail until the next signal.
  useEffect(() => {
    if (!saved) return;
    let live = true;
    void ready()
      .then(() => kernelImport(saved.kernel))
      .then((s) => {
        if (!live) return;
        setSnap(s);
        setLiked(new Set(saved.liked));
        setDisliked(new Set(saved.disliked));
        setVersion((v) => v + 1);
        setRailVersion((v) => v + 1);
      })
      .catch(() => { /* no snapshot, no restore — the app already reports load errors */ });
    return () => { live = false; };
  }, [saved]);

  const seed = useCallback(async (genres: string[], artists: string[]) => {
    const s = await kernelSeed(genres, artists);
    setSnap(s);
    setVersion((v) => v + 1);
    setRailVersion((v) => v + 1);
    setOnboarded(true);
  }, []);

  // Persist after every signal. Reads liked/disliked from state rather than from
  // inside `signal`, whose [] deps would close over the sets as they were at mount.
  useEffect(() => {
    if (version === 0) return;
    void kernelExport()
      .then((kernel) => save({ dim: D, kernel, liked: [...liked], disliked: [...disliked] }))
      .catch(() => {});
  }, [version, liked, disliked]);

  const signal = useCallback((id: string, kind: SignalKind) => {
    if (kind === 'like') setLiked((s) => new Set(s).add(id));
    if (kind === 'dislike') setDisliked((s) => new Set(s).add(id));
    void kernelSignal(id, kind).then((s) => {
      setSnap(s);
      setVersion((v) => v + 1);
      // A thumb is a deliberate statement and should be answered at once. `play`
      // and `skip` are not: both fire from the same click on a different card,
      // so honouring them here would move the grid out from under that click.
      if (kind === 'like' || kind === 'dislike') setRailVersion((v) => v + 1);
    });
  }, []);

  const settle = useCallback(() => setRailVersion((v) => v + 1), []);

  // Reset. The version bump drives the effect above, which writes the now-empty
  // kernel back — and `load()` treats an empty kernel as nothing to restore, so
  // that is equivalent to clearing the key without a second code path for it.
  const refresh = useCallback((s: KernelSnapshot) => {
    setSnap(s);
    setLiked(new Set());
    setDisliked(new Set());
    setVersion((v) => v + 1);
    setRailVersion((v) => v + 1);
  }, []);

  const value = useMemo<KernelCtx>(
    () => ({
      snap, version, railVersion, signal, settle,
      onboarded, seed, liked, disliked, refresh,
    }),
    [snap, version, railVersion, signal, settle, onboarded, seed, liked, disliked, refresh],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
