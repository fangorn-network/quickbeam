// Per-entity-type visual identity (DESIGN.md §3) + glossary (LANGUAGE.md §1).
import type { EntityType } from './types';

export interface EntityMeta {
  type: EntityType;
  accentVar: string; // CSS custom property name
  letter: string; // left-rail / badge abbreviation
  singular: string;
  plural: string;
  icon: string; // small inline glyph used in tiles
  ledeVerb: string; // "is a" | "was"
  definition: string;
}

export const ENTITY_META: Record<EntityType, EntityMeta> = {
  Artist: {
    type: 'Artist',
    accentVar: '--accent-artist',
    letter: 'A',
    singular: 'Artist',
    plural: 'Artists',
    icon: '🎤',
    ledeVerb: 'is a',
    definition:
      'A person, group, or character who creates or performs music, ranging from solo musicians and bands to orchestras and fictional personas.',
  },
  Recording: {
    type: 'Recording',
    accentVar: '--accent-recording',
    letter: 'R',
    singular: 'Recording',
    plural: 'Recordings',
    icon: '≋',
    ledeVerb: 'is a',
    definition:
      'A specific audio (or video) capture of a musical performance, uniquely identified by its duration and ISRC codes.',
  },
  Release: {
    type: 'Release',
    accentVar: '--accent-release',
    letter: 'Re',
    singular: 'Release',
    plural: 'Releases',
    icon: '💿',
    ledeVerb: 'is a',
    definition:
      'A physical or digital edition of an album, single, or EP — the concrete product that was distributed on a given date by a specific label.',
  },
  ReleaseGroup: {
    type: 'ReleaseGroup',
    accentVar: '--accent-releasegroup',
    letter: 'RG',
    singular: 'Album',
    plural: 'Albums',
    icon: '🗂',
    ledeVerb: 'is a',
    definition:
      'The abstract musical work that groups all editions of an album or single together.',
  },
  Work: {
    type: 'Work',
    accentVar: '--accent-work',
    letter: 'W',
    singular: 'Work',
    plural: 'Works',
    icon: '✏',
    ledeVerb: 'is a',
    definition:
      'The underlying musical or lyrical composition — the song as written, independent of any particular performance or release.',
  },
  Place: {
    type: 'Place',
    accentVar: '--accent-place',
    letter: 'Pl',
    singular: 'Place',
    plural: 'Places',
    icon: '📌',
    ledeVerb: 'is a',
    definition:
      'A physical location associated with music-making: a recording studio, concert hall, venue, or other facility.',
  },
  Event: {
    type: 'Event',
    accentVar: '--accent-event',
    letter: 'Ev',
    singular: 'Event',
    plural: 'Events',
    icon: '📅',
    ledeVerb: 'was',
    definition:
      'A dated live occurrence — a concert, festival, or broadcast — at which artists performed for an audience.',
  },
  Area: {
    type: 'Area',
    accentVar: '--accent-area',
    letter: 'Ar',
    singular: 'Area',
    plural: 'Areas',
    icon: '🌐',
    ledeVerb: 'is a',
    definition:
      'A geographic region — country, city, or subdivision — that contextualises where artists, events, and recordings originate.',
  },
  Instrument: {
    type: 'Instrument',
    accentVar: '--accent-instrument',
    letter: 'In',
    singular: 'Instrument',
    plural: 'Instruments',
    icon: '🎸',
    ledeVerb: 'is a',
    definition:
      'A musical instrument or instrument family catalogued in MusicBrainz, including its lineage and variants.',
  },
};

export function metaFor(type: string): EntityMeta | undefined {
  return ENTITY_META[type as EntityType];
}

export function accentColor(type: string): string {
  const m = metaFor(type);
  return m ? `var(${m.accentVar})` : 'var(--text-secondary)';
}
