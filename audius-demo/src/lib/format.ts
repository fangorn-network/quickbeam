import { CONTENT_NODE, PLATFORM_OWNER } from './config';
import type { Rec } from './types';

export const isPlatform = (owner?: string) => (owner ?? '').toLowerCase() === PLATFORM_OWNER;

export const shortAddr = (a?: string) =>
  !a ? '' : a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a;

/** Artwork resolves from the CID stored in the graph — not a baked-in mirror URL. */
export function artUrl(cid?: string, size: 150 | 480 | 1000 = 480): string | null {
  return cid ? `${CONTENT_NODE}/content/${cid}/${size}x${size}.jpg` : null;
}

export function recArt(r: Rec, size: 150 | 480 | 1000 = 480): string | null {
  const f = r.fields;
  return artUrl((f.artworkCid as string) || (f.avatarCid as string), size);
}

export const nfmt = (n?: number) =>
  n == null ? '' :
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` :
  n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(n);

export function duration(sec?: number) {
  if (!sec) return '';
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

export const splitTags = (s?: string) =>
  (s ?? '').split(',').map((t) => t.trim()).filter(Boolean);

/** Relation names are the graph's own vocabulary; show them as English. */
const REL_LABEL: Record<string, { out: string; in: string }> = {
  created:    { out: 'Tracks',            in: 'Created by' },
  released:   { out: 'Albums',            in: 'Released by' },
  curated:    { out: 'Playlists',         in: 'Curated by' },
  contains:   { out: 'Tracks in this',    in: 'Appears in' },
  follows:    { out: 'Following',         in: 'Followers' },
  relatedTo:  { out: 'Related artists',   in: 'Related to' },
  supports:   { out: 'Supports',          in: 'Supported by' },
  reposted:   { out: 'Reposted',          in: 'Reposted by' },
  favorited:  { out: 'Favorited',         in: 'Favorited by' },
  worksIn:    { out: 'Works in',          in: 'Artists' },
  remixOf:    { out: 'Remix of',          in: 'Remixed by' },
  hasStem:    { out: 'Stems',             in: 'Stem of' },
  inGenre:    { out: 'Genre',             in: 'Tracks' },
  hasMood:    { out: 'Mood',              in: 'Tracks' },
  taggedWith: { out: 'Tagged',            in: 'Tagged tracks' },
  sameAs:     { out: 'Same artist as',    in: 'Same artist as' },
};

export function relLabel(rel: string, dir: 'out' | 'in') {
  return REL_LABEL[rel]?.[dir] ?? `${rel} (${dir})`;
}

/** A stable hue per id, so a record with no cover still reads as its own object. */
export function fallbackArt(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
  return `linear-gradient(315deg, hsl(${h} 62% 34%) 0%, hsl(${(h + 42) % 360} 68% 46%) 100%)`;
}

export const initial = (s?: string) => (s ?? '?').trim().charAt(0).toUpperCase() || '?';
