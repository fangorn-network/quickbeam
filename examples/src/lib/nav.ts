// Centralised href + PageRef builders.
import type { EntitySummary, PageRef } from './types';

export function entityHref(pointId: string): string {
  return `/entity/${encodeURIComponent(pointId)}`;
}

export function searchHref(q: string, type?: string): string {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (type) params.set('type', type);
  return `/search?${params.toString()}`;
}

export function browseHref(type: string): string {
  return `/browse/${encodeURIComponent(type)}`;
}

// Geographic proximity search: rank entries by distance from a "lat,lng" point.
export function nearHref(coords: string): string {
  return `/search?near=${encodeURIComponent(coords)}`;
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
