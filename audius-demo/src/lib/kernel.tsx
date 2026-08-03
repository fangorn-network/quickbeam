// Main-thread handle on the session kernel. The kernel itself lives in the worker,
// beside the vectors; this holds only its snapshot and a version counter that tells
// the rail when to re-query.
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { kernelSignal, type KernelSnapshot, type SignalKind } from './client';

interface KernelCtx {
  snap: KernelSnapshot | null;
  /** Bumped on every signal; the rail re-queries when it changes. */
  version: number;
  signal: (id: string, kind: SignalKind) => void;
  /** Records signalled on this session, so controls can show their state. */
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
    snap: null, version: 0, signal: () => {},
    liked: new Set(), disliked: new Set(), refresh: () => {},
  };
}

export function KernelProvider({ children }: { children: React.ReactNode }) {
  const [snap, setSnap] = useState<KernelSnapshot | null>(null);
  const [version, setVersion] = useState(0);
  const [liked, setLiked] = useState<Set<string>>(new Set());
  const [disliked, setDisliked] = useState<Set<string>>(new Set());

  const signal = useCallback((id: string, kind: SignalKind) => {
    if (kind === 'like') setLiked((s) => new Set(s).add(id));
    if (kind === 'dislike') setDisliked((s) => new Set(s).add(id));
    void kernelSignal(id, kind).then((s) => {
      setSnap(s);
      setVersion((v) => v + 1);
    });
  }, []);

  const refresh = useCallback((s: KernelSnapshot) => {
    setSnap(s);
    setLiked(new Set());
    setDisliked(new Set());
    setVersion((v) => v + 1);
  }, []);

  const value = useMemo<KernelCtx>(
    () => ({ snap, version, signal, liked, disliked, refresh }),
    [snap, version, signal, liked, disliked, refresh],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
