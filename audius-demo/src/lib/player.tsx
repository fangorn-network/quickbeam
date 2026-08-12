// Playback: ONE <audio> element for the whole app.
//
// The obvious implementation — an <audio> per card — is how this usually goes wrong:
// dozens of media elements, each holding a connection, and no single source of truth
// for "what is playing". There is exactly one here, owned by this provider.
//
// It costs the main thread nothing. Decoding audio happens on the browser's own media
// pipeline, not in JS, so the worker architecture that keeps search off the main
// thread is unaffected — the search spinner keeps spinning while audio plays.
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from 'react';
import { APP_NAME, DISCOVERY_NODE } from './config';
import { useKernel } from './kernel';
import type { Rec } from './types';

/** `fields.id` is the Audius track id, which is all the stream URL needs. */
export function streamUrl(rec: Rec): string | null {
  const id = rec.fields.id;
  return id ? `${DISCOVERY_NODE}/v1/tracks/${id}/stream?app_name=${encodeURIComponent(APP_NAME)}` : null;
}

export const isPlayable = (rec: Rec) => rec.entityType === 'Track' && !!rec.fields.id;

interface PlayerState {
  current: Rec | null;
  playing: boolean;
  loading: boolean;
  time: number;
  duration: number;
  /** Ids that failed to load — ~1% of tracks are stream-gated or withdrawn. */
  unavailable: Set<string>;
  play: (rec: Rec, queue?: Rec[]) => void;
  toggle: (rec?: Rec) => void;
  seek: (t: number) => void;
  next: () => void;
  prev: () => void;
}

const Ctx = createContext<PlayerState | null>(null);
export const usePlayer = () => {
  const p = useContext(Ctx);
  if (!p) throw new Error('usePlayer outside PlayerProvider');
  return p;
};

export function PlayerProvider({ children }: { children: React.ReactNode }) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [current, setCurrent] = useState<Rec | null>(null);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [unavailable, setUnavailable] = useState<Set<string>>(new Set());
  const queueRef = useRef<Rec[]>([]);
  const kernel = useKernel();

  // Did the last track finish on its own, or did the user cut it short?
  //
  // The kernel treats those as opposite evidence, so conflating them poisons the
  // signal: a track you listened to all the way through would be recorded as a
  // rejection. Same flag sonder keeps in usePlaybackRouter.
  const naturalEndRef = useRef(false);
  const currentRef = useRef<Rec | null>(null);
  currentRef.current = current;

  // Created imperatively rather than rendered, so React never re-mounts it and
  // restarts playback on an unrelated state change.
  if (!audioRef.current && typeof Audio !== 'undefined') {
    const a = new Audio();
    a.preload = 'none';   // nothing is fetched until the first play
    audioRef.current = a;
  }

  const start = useCallback((rec: Rec, queue?: Rec[]) => {
    const a = audioRef.current;
    const url = streamUrl(rec);
    if (!a || !url) return;
    if (queue) queueRef.current = queue.filter(isPlayable);

    // Leaving a different track before it ended is a skip; reaching the end is not.
    const prev = currentRef.current;
    if (prev && prev.id !== rec.id && !naturalEndRef.current) {
      kernel.signal(prev.id, 'skip');
    }
    naturalEndRef.current = false;
    kernel.signal(rec.id, 'play');

    setCurrent(rec);
    setLoading(true);
    setTime(0);
    // The graph already knows the duration, so the scrubber is correctly scaled
    // before any audio metadata arrives.
    setDuration(Number(rec.fields.duration) || 0);
    a.src = url;
    void a.play().catch(() => {
      // Autoplay policy can't bite here (playback always starts from a click), so a
      // rejection means the stream itself refused — gated or withdrawn.
      setUnavailable((s) => new Set(s).add(rec.id));
      setLoading(false);
      setPlaying(false);
    });
  }, [kernel]);

  /** Move through the queue the current track was played from. Wraps at both ends. */
  const step = useCallback((delta: number) => {
    const q = queueRef.current;
    if (!current || q.length < 2) return;
    const i = q.findIndex((t) => t.id === current.id);
    if (i < 0) return;
    const target = q[(i + delta + q.length) % q.length];
    if (target) start(target, q);
  }, [current, start]);

  useEffect(() => {
    const a = audioRef.current;
    if (!a) return;
    const onPlay = () => { setPlaying(true); setLoading(false); };
    const onPause = () => setPlaying(false);
    const onTime = () => setTime(a.currentTime);
    const onMeta = () => { if (Number.isFinite(a.duration)) setDuration(a.duration); };
    const onEnded = () => {
      // Consumed by `start` on the next track, which is why it's set before step().
      naturalEndRef.current = true;
      setPlaying(false);
      // Hearing a track out is the settled evidence the recommendation rail waits
      // for — the kernel already took the play signal when this track started.
      kernel.settle();
      step(1);
    };
    const onError = () => {
      setLoading(false);
      setPlaying(false);
      setCurrent((c) => { if (c) setUnavailable((s) => new Set(s).add(c.id)); return c; });
    };
    a.addEventListener('play', onPlay);
    a.addEventListener('playing', onPlay);
    a.addEventListener('pause', onPause);
    a.addEventListener('timeupdate', onTime);
    a.addEventListener('loadedmetadata', onMeta);
    a.addEventListener('ended', onEnded);
    a.addEventListener('error', onError);
    return () => {
      a.removeEventListener('play', onPlay);
      a.removeEventListener('playing', onPlay);
      a.removeEventListener('pause', onPause);
      a.removeEventListener('timeupdate', onTime);
      a.removeEventListener('loadedmetadata', onMeta);
      a.removeEventListener('ended', onEnded);
      a.removeEventListener('error', onError);
    };
  }, [step, kernel]);

  const toggle = useCallback((rec?: Rec) => {
    const a = audioRef.current;
    if (!a) return;
    if (rec && rec.id !== current?.id) { start(rec); return; }
    if (a.paused) void a.play().catch(() => setPlaying(false));
    else a.pause();
  }, [current, start]);

  const seek = useCallback((t: number) => {
    const a = audioRef.current;
    if (a && Number.isFinite(t)) { a.currentTime = t; setTime(t); }
  }, []);

  // Space toggles playback — but only when focus isn't in a text field, or typing a
  // search query would start and stop the music.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || !current) return;
      const el = document.activeElement;
      const typing = el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
        || (el as HTMLElement | null)?.isContentEditable;
      if (typing) return;
      e.preventDefault();
      toggle();
    };
    addEventListener('keydown', onKey);
    return () => removeEventListener('keydown', onKey);
  }, [current, toggle]);

  const value = useMemo<PlayerState>(() => ({
    current, playing, loading, time, duration, unavailable,
    play: start, toggle, seek,
    next: () => step(1), prev: () => step(-1),
  }), [current, playing, loading, time, duration, unavailable, start, toggle, seek, step]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
