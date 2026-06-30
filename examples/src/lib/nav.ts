// Centralised href + PageRef builders.
import type { EntitySummary, PageRef } from './types';

export function entityHref(pointId: string): string {
  return `/entity/${encodeURIComponent(pointId)}`;
}

// All discovery now lives on the unified /discover surface; the lens (?lens=)
// selects List / Map / Answer over the same query + result set.
export function searchHref(q: string, type?: string): string {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (type) params.set('type', type);
  return `/discover?${params.toString()}`;
}

export function browseHref(type: string): string {
  return `/browse/${encodeURIComponent(type)}`;
}

// Open the concierge (Answer lens) on Discover, optionally pre-seeded with a query.
export function askHref(q?: string): string {
  const params = new URLSearchParams({ lens: 'answer' });
  if (q) params.set('q', q);
  return `/discover?${params.toString()}`;
}

// Geographic proximity search: rank entries by distance from a "lat,lng" point.
export function nearHref(coords: string): string {
  return `/discover?near=${encodeURIComponent(coords)}`;
}

// Open the Map lens focused on a single entity (centers + rings its pin).
export function mapFocusHref(pointId: string): string {
  return `/discover?focus=${encodeURIComponent(pointId)}&lens=map`;
}

// External Google Maps turn-by-turn directions to a "lat,lng" point. Works for
// every entity that carries coordinates (Google + OSM trails/lakes/landmarks),
// not just the Google-sourced ones that happen to have a googleMapsUri. Uses the
// universal cross-platform Maps URL scheme so it opens the native app on mobile.
export function directionsHref(coords: string): string {
  return `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(coords.trim())}`;
}

export function nearPageRef(coords: string, label: string): PageRef {
  return {
    kind: 'search',
    label: `Near ${label}`,
    query: coords,
    href: nearHref(coords),
  };
}

export function entityPageRef(e: EntitySummary): PageRef {
  return {
    kind: 'entity',
    pointId: e.pointId,
    entityType: e.entityType,
    label: e.title,
    href: entityHref(e.pointId),
  };
}

export function searchPageRef(q: string, type?: string): PageRef {
  return {
    kind: 'search',
    entityType: type,
    label: `"${q}"`,
    query: q,
    href: searchHref(q, type),
  };
}
