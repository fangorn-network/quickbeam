// A hash router in a dozen lines. Three views don't justify a routing library, and
// hash routes mean the built app works from any static host (or file://) with no
// server rewrite rules.
import { useEffect, useState } from 'react';

export interface Route { view: 'home' | 'search' | 'entity'; q: string; id: string }

export function parse(hash: string): Route {
  const h = hash.replace(/^#\/?/, '');
  const [path, query = ''] = h.split('?');
  const params = new URLSearchParams(query);
  if (path === 'search') return { view: 'search', q: params.get('q') ?? '', id: '' };
  // Entity ids contain ':' and '/', so they ride as a query param, not a path segment.
  if (path === 'e') return { view: 'entity', q: '', id: params.get('id') ?? '' };
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
