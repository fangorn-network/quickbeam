// Centralised href + PageRef builders.
import type { EntitySummary, PageRef } from './types';
import { secondaryLine } from './summary';

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

export function summaryFor(e: EntitySummary): string {
  return secondaryLine(e);
}
