// A hash router in a dozen lines. Three views don't justify a routing library, and
// hash routes mean the built app works from any static host (or file://) with no
// server rewrite rules.
import { useEffect, useState } from 'react';

export interface Route {
  view: 'home' | 'search' | 'entity' | 'about' | 'privacy' | 'playlists';
  q: string;
  id: string;
  /** An opaque share payload (see playlists.ts toShare). Optional so the five routes
   *  that cannot carry one stay untouched. */
  share?: string;
}

export function parse(hash: string): Route {
  const h = hash.replace(/^#\/?/, '');
  const [path, query = ''] = h.split('?');
  const params = new URLSearchParams(query);
  if (path === 'search') return { view: 'search', q: params.get('q') ?? '', id: '' };
  // Entity ids contain ':' and '/', so they ride as a query param, not a path segment.
  if (path === 'e') return { view: 'entity', q: '', id: params.get('id') ?? '' };
  // Index, detail and a shared link are one route; `id` is empty for the index, and
  // `s` wins over both — it addresses a playlist that is not in this browser yet.
  if (path === 'playlists') {
    return { view: 'playlists', q: '', id: params.get('id') ?? '', share: params.get('s') ?? '' };
  }
  if (path === 'about') return { view: 'about', q: '', id: '' };
  if (path === 'privacy') return { view: 'privacy', q: '', id: '' };
  return { view: 'home', q: '', id: '' };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(location.hash));
  useEffect(() => {
    const on = () => setRoute(parse(location.hash));
    addEventListener('hashchange', on);
    return () => removeEventListener('hashchange', on);
  }, []);
  return route;
}

export const goSearch = (q: string) => { location.hash = `#/search?q=${encodeURIComponent(q)}`; };
export const goEntity = (id: string) => { location.hash = `#/e?id=${encodeURIComponent(id)}`; };
export const goHome = () => { location.hash = '#/'; };
export const goAbout = () => { location.hash = '#/about'; };
export const goPrivacy = () => { location.hash = '#/privacy'; };
export const goPlaylists = (id?: string) => {
  location.hash = id ? `#/playlists?id=${encodeURIComponent(id)}` : '#/playlists';
};

/** An absolute link to a shared playlist. The payload rides in the HASH, which is the
 *  whole privacy story: browsers never transmit the part after `#`, so handing someone
 *  this link puts nothing in anyone's server logs. Deliberately takes an opaque string
 *  rather than a Playlist — router.ts is imported by everything, so importing the
 *  playlist store back into it would invert the dependency. */
export const shareHref = (payload: string) =>
  `${location.origin}${location.pathname}#/playlists?s=${payload}`;
