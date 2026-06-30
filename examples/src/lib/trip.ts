// "My Trip" — a client-side, shareable itinerary. The thesis: the social object is
// built and shared WITHOUT a backend. The trip lives in localStorage on-device, and
// a share link encodes it into the URL *hash fragment* (`#trip=…`) — which browsers
// never send to the server, so even the static host's logs never see who shared
// which trip. The recipient's browser decodes the fragment and rehydrates each item
// from the already-loaded public shard data (items are just IDs + a display title).
//
// See docs/SOCIAL_ROADMAP.md, Phase 1.
import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useState,
} from 'react';
import type { ReactNode } from 'react';
import type { EntitySummary } from './types';

export interface TripItem {
  id: string; // pointId — the routing id, resolvable against the loaded snapshot
  type: string; // entityType (Business / Event / …)
  title: string; // display label, so a shared trip renders without re-resolving
}

const STORAGE_KEY = 'sond3r.trip.v1';
// Dislikes — the negative half of the like/dislike signal that drives the session
// kernel (see sessionKernel.ts). Kept beside the trip ("liked") list but in its own
// key, and never shared (a share link is the things you liked, not the ones you didn't).
const DISLIKE_KEY = 'sond3r.dislikes.v1';
export const SHARE_HASH_KEY = 'trip'; // location.hash → `#trip=<payload>`

// ---- share encoding (base64url over a compact tuple form) ----
// Unicode-safe: TextEncoder → bytes → base64 → url-safe alphabet.
function toB64Url(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function fromB64Url(s: string): string {
  const b64 = s.replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(b64);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

// items → `[id, type, title][]` (tuples keep the link short) → base64url.
export function encodeTrip(items: TripItem[]): string {
  return toB64Url(JSON.stringify(items.map((it) => [it.id, it.type, it.title])));
}

export function decodeTrip(payload: string): TripItem[] | null {
  try {
    const arr = JSON.parse(fromB64Url(payload));
    if (!Array.isArray(arr)) return null;
    return arr
      .filter((t) => Array.isArray(t) && typeof t[0] === 'string' && t[0])
      .map((t) => ({
        id: String(t[0]),
        type: String(t[1] ?? 'Unknown'),
        title: String(t[2] ?? t[0]),
      }));
  } catch {
    return null;
  }
}

// Read a shared trip out of the current location hash, if any.
export function sharedTripFromHash(hash: string): TripItem[] | null {
  const m = hash.replace(/^#/, '').match(new RegExp(`(?:^|&)${SHARE_HASH_KEY}=([^&]+)`));
  return m ? decodeTrip(m[1]) : null;
}

export function tripItemFromSummary(e: EntitySummary): TripItem {
  return { id: e.pointId, type: e.entityType, title: e.title };
}

// ---- storage ----
function readStored(key: string): TripItem[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? (arr as TripItem[]).filter((t) => t && typeof t.id === 'string') : [];
  } catch {
    return [];
  }
}

// ---- context ----
interface TripCtx {
  items: TripItem[];
  has: (id: string) => boolean;
  add: (item: TripItem) => void;
  remove: (id: string) => void;
  toggle: (item: TripItem) => void;
  move: (id: string, dir: -1 | 1) => void;
  clear: () => void;
  /** Build an on-thesis share URL (payload in the unsent hash fragment). */
  shareUrl: () => string;
  // ---- dislikes (the negative session signal) ----
  dislikes: TripItem[];
  isDisliked: (id: string) => boolean;
  toggleDislike: (item: TripItem) => void;
}

const Ctx = createContext<TripCtx | null>(null);

export function TripProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<TripItem[]>(() => readStored(STORAGE_KEY));
  const [dislikes, setDislikes] = useState<TripItem[]>(() => readStored(DISLIKE_KEY));

  // Persist on every change.
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
    } catch {
      /* storage full / blocked — trip just won't persist this session */
    }
  }, [items]);
  useEffect(() => {
    try {
      localStorage.setItem(DISLIKE_KEY, JSON.stringify(dislikes));
    } catch {
      /* storage full / blocked */
    }
  }, [dislikes]);

  const has = useCallback((id: string) => items.some((it) => it.id === id), [items]);

  const add = useCallback((item: TripItem) => {
    // Liking clears any prior dislike — the two signals are mutually exclusive.
    setDislikes((prev) => prev.filter((it) => it.id !== item.id));
    setItems((prev) => (prev.some((it) => it.id === item.id) ? prev : [...prev, item]));
  }, []);

  const remove = useCallback((id: string) => {
    setItems((prev) => prev.filter((it) => it.id !== id));
  }, []);

  const toggle = useCallback((item: TripItem) => {
    setItems((prev) => {
      if (prev.some((it) => it.id === item.id)) return prev.filter((it) => it.id !== item.id);
      setDislikes((dl) => dl.filter((it) => it.id !== item.id)); // liking clears a dislike
      return [...prev, item];
    });
  }, []);

  const isDisliked = useCallback((id: string) => dislikes.some((it) => it.id === id), [dislikes]);

  const toggleDislike = useCallback((item: TripItem) => {
    setDislikes((prev) => {
      if (prev.some((it) => it.id === item.id)) return prev.filter((it) => it.id !== item.id);
      setItems((it) => it.filter((x) => x.id !== item.id)); // disliking clears a like
      return [...prev, item];
    });
  }, []);

  const move = useCallback((id: string, dir: -1 | 1) => {
    setItems((prev) => {
      const i = prev.findIndex((it) => it.id === id);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  }, []);

  const clear = useCallback(() => setItems([]), []);

  const shareUrl = useCallback(() => {
    const { origin } = window.location;
    return `${origin}/trip#${SHARE_HASH_KEY}=${encodeTrip(items)}`;
  }, [items]);

  const value: TripCtx = {
    items, has, add, remove, toggle, move, clear, shareUrl,
    dislikes, isDisliked, toggleDislike,
  };
  return createElement(Ctx.Provider, { value }, children);
}

export function useTrip(): TripCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error('useTrip must be used within <TripProvider>');
  return c;
}
