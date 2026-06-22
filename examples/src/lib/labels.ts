// Field label map + display formatting, from LANGUAGE.md §2.
import type { EntityType } from './types';

export interface FieldLabel {
  label: string;
  type: 'string' | 'number' | 'boolean' | 'link';
}

// Human labels for fields. Fields that are rendered specially (title, text,
// entityType, schemaVersion) are intentionally omitted / marked suppressed.
export const FIELD_LABELS: Record<string, FieldLabel> = {
  mbid: { label: 'MusicBrainz ID', type: 'string' },
  tags: { label: 'Tags', type: 'string' },
  disambiguation: { label: 'Note', type: 'string' },
  beginYear: { label: 'Active from', type: 'string' },
  endYear: { label: 'Active until', type: 'string' },
  rating: { label: 'Community rating', type: 'number' },
  area: { label: 'Origin / Location', type: 'link' },
  artistType: { label: 'Type', type: 'string' },
  gender: { label: 'Gender', type: 'string' },
  sortName: { label: 'Sort name', type: 'string' },
  byArtist: { label: 'By', type: 'link' },
  durationMs: { label: 'Length', type: 'number' },
  isrcCodes: { label: 'ISRC', type: 'string' },
  video: { label: 'Video recording', type: 'boolean' },
  datePublished: { label: 'Date', type: 'string' },
  labelName: { label: 'Label', type: 'string' },
  status: { label: 'Status', type: 'string' },
  barcode: { label: 'Barcode', type: 'string' },
  primaryType: { label: 'Format', type: 'string' },
  iswcCodes: { label: 'ISWC', type: 'string' },
  workType: { label: 'Work type', type: 'string' },
  eventType: { label: 'Event type', type: 'string' },
  time: { label: 'Time', type: 'string' },
  setlist: { label: 'Setlist', type: 'string' },
  cancelled: { label: 'Cancelled', type: 'boolean' },
  placeType: { label: 'Venue type', type: 'string' },
  address: { label: 'Address', type: 'string' },
  coordinates: { label: 'Location', type: 'string' },
  areaType: { label: 'Area type', type: 'string' },
  instrumentType: { label: 'Instrument type', type: 'string' },
  description: { label: 'Description', type: 'string' },
};

// Fields that are never shown as labelled rows in the FieldTable.
export const SUPPRESSED_FIELDS = new Set([
  'title',
  'text',
  'entityType',
  'schemaVersion',
  'description', // rendered as body text in lede area for Instrument
]);

// Fields whose values are name-strings → clicking runs a SEARCH (soft link).
export const SOFT_LINK_FIELDS = new Set(['byArtist', 'area']);

export function fieldLabel(key: string): string {
  return FIELD_LABELS[key]?.label ?? humanise(key);
}

// Humanise a raw key/rel: hyphens/underscores → spaces, title-case.
export function humanise(raw: string): string {
  // also split camelCase
  const spaced = raw
    .replace(/[-_]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2');
  return spaced
    .split(/\s+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

// ---- Display formatting helpers (LANGUAGE.md §2) ----

export function formatDuration(ms: number): string {
  const totalSec = Math.round(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function formatRating(r: number): string {
  return `${r.toFixed(1)} / 5`;
}

export function formatActive(
  beginYear?: string | number,
  endYear?: string | number,
): string | null {
  const b = beginYear != null && `${beginYear}`.length ? `${beginYear}` : null;
  const e = endYear != null && `${endYear}`.length ? `${endYear}` : null;
  if (b && e) return `Active · ${b}–${e}`;
  if (b && !e) return `Active · ${b}–present`;
  if (!b && e) return `Dissolved · ${e}`;
  return null;
}

export function splitList(v: string): string[] {
  return v
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

// MusicBrainz URL patterns (LANGUAGE.md §5).
export const MB_URL_SEGMENT: Record<string, string> = {
  Artist: 'artist',
  Recording: 'recording',
  Release: 'release',
  ReleaseGroup: 'release-group',
  Work: 'work',
  Event: 'event',
  Place: 'place',
  Area: 'area',
  Instrument: 'instrument',
};

export function mbUrl(entityType: string, mbid: string): string | null {
  const seg = MB_URL_SEGMENT[entityType];
  if (!seg) return null;
  return `https://musicbrainz.org/${seg}/${mbid}`;
}

// Field rendering order priority (lower = earlier). Unknown fields sort last.
const FIELD_ORDER = [
  'area',
  'byArtist',
  'artistType',
  'gender',
  'sortName',
  'beginYear',
  'endYear',
  'primaryType',
  'status',
  'labelName',
  'datePublished',
  'durationMs',
  'workType',
  'eventType',
  'time',
  'placeType',
  'areaType',
  'instrumentType',
  'address',
  'coordinates',
  'rating',
  'isrcCodes',
  'iswcCodes',
  'barcode',
  'video',
  'cancelled',
  'setlist',
  'disambiguation',
  'tags',
  'mbid',
];

export function fieldSortKey(key: string): number {
  const i = FIELD_ORDER.indexOf(key);
  return i === -1 ? FIELD_ORDER.length + 1 : i;
}

// Projection list fields used for the Connections section.
export const LIST_FIELDS = [
  'artists',
  'events',
  'recordings',
  'places',
  'works',
  'releases',
  'tracks',
] as const;

// Map an entity type to a plural display noun (LANGUAGE.md §1).
export const PLURAL_NOUN: Record<EntityType, string> = {
  Artist: 'Artists',
  Recording: 'Recordings',
  Release: 'Releases',
  ReleaseGroup: 'Albums',
  Work: 'Works',
  Place: 'Places',
  Event: 'Events',
  Area: 'Areas',
  Instrument: 'Instruments',
};

export const SINGULAR_NOUN: Record<EntityType, string> = {
  Artist: 'Artist',
  Recording: 'Recording',
  Release: 'Release',
  ReleaseGroup: 'Album',
  Work: 'Work',
  Place: 'Place',
  Event: 'Event',
  Area: 'Area',
  Instrument: 'Instrument',
};
