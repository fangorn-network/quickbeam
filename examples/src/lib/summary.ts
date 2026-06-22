// Builds the one-line "secondary" description shown under a title in cards,
// rails, and the command palette. Type-aware.
import type { EntitySummary } from './types';
import { formatDuration, formatActive } from './labels';

export function secondaryLine(e: EntitySummary): string {
  const f = e.fields;
  const parts: string[] = [];
  const s = (v: unknown): string | null =>
    typeof v === 'string' && v.trim() ? v.trim() : null;

  switch (e.entityType) {
    case 'Artist': {
      const at = s(f.artistType);
      const area = s(f.area);
      const active = formatActive(
        f.beginYear as string | undefined,
        f.endYear as string | undefined,
      );
      if (area) parts.push(area);
      if (at) parts.push(at);
      if (active) parts.push(active.replace('Active · ', ''));
      break;
    }
    case 'Recording': {
      const by = s(f.byArtist);
      if (by) parts.push(by);
      if (typeof f.durationMs === 'number' && f.durationMs > 0)
        parts.push(formatDuration(f.durationMs));
      break;
    }
    case 'Release':
    case 'ReleaseGroup': {
      const by = s(f.byArtist);
      if (by) parts.push(by);
      const t = s(f.primaryType) ?? s(f.status);
      if (t) parts.push(t);
      const d = s(f.datePublished);
      if (d) parts.push(d);
      break;
    }
    case 'Work': {
      const wt = s(f.workType);
      if (wt) parts.push(wt);
      break;
    }
    case 'Event': {
      const et = s(f.eventType);
      if (et) parts.push(et);
      const d = s(f.datePublished);
      if (d) parts.push(d);
      break;
    }
    case 'Place': {
      const pt = s(f.placeType);
      if (pt) parts.push(pt);
      const area = s(f.area);
      if (area) parts.push(area);
      break;
    }
    case 'Area': {
      const at = s(f.areaType);
      if (at) parts.push(at);
      break;
    }
    case 'Instrument': {
      const it = s(f.instrumentType);
      if (it) parts.push(it);
      break;
    }
  }

  if (parts.length === 0) {
    const dis = s(f.disambiguation);
    if (dis) parts.push(dis);
  }
  return parts.join(' · ');
}
