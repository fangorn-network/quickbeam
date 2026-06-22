// Relationship phrasing, from LANGUAGE.md §3.
import { humanise } from './labels';

interface RelPhrase {
  forward: string; // reading from the `from` entity's page
  inverse: string; // reading from the `to` entity's page
}

// Keyed by `${rel}|${from}|${to}`. The core ~25 high-frequency rels.
export const REL_PHRASES: Record<string, RelPhrase> = {
  'byArtist|Recording|Artist': {
    forward: '{from} was recorded by {to}',
    inverse: '{to} is the credited artist on {from}',
  },
  'byArtist|Release|Artist': {
    forward: '{from} was released by {to}',
    inverse: '{to} released {from}',
  },
  'byArtist|ReleaseGroup|Artist': {
    forward: '{from} was released by {to}',
    inverse: '{to} released the album {from}',
  },
  'hasTrack|Release|Recording': {
    forward: '{from} includes the track {to}',
    inverse: '{to} appears on {from}',
  },
  'hasRelease|ReleaseGroup|Release': {
    forward: '{from} has the edition {to}',
    inverse: '{to} is an edition of the album {from}',
  },
  'performance|Recording|Work': {
    forward: '{from} is a performance of {to}',
    inverse: '{to} was performed in {from}',
  },
  'composer|Artist|Work': {
    forward: '{from} composed {to}',
    inverse: '{to} was composed by {from}',
  },
  'writer|Artist|Work': {
    forward: '{from} wrote {to}',
    inverse: '{to} was written by {from}',
  },
  'lyricist|Artist|Work': {
    forward: '{from} wrote the lyrics for {to}',
    inverse: "{to}'s lyrics were written by {from}",
  },
  'performer|Artist|Recording': {
    forward: '{from} performed on {to}',
    inverse: '{to} features {from} as performer',
  },
  'vocal|Artist|Recording': {
    forward: '{from} provided vocals on {to}',
    inverse: '{to} features vocals by {from}',
  },
  'instrument|Artist|Recording': {
    forward: '{from} played on {to}',
    inverse: '{to} features {from} on instrument',
  },
  'producer|Artist|Recording': {
    forward: '{from} produced {to}',
    inverse: '{to} was produced by {from}',
  },
  'engineer|Artist|Recording': {
    forward: '{from} engineered {to}',
    inverse: '{to} was engineered by {from}',
  },
  'mix|Artist|Recording': {
    forward: '{from} mixed {to}',
    inverse: '{to} was mixed by {from}',
  },
  'conductor|Artist|Recording': {
    forward: '{from} conducted {to}',
    inverse: '{to} was conducted by {from}',
  },
  'remixer|Artist|Recording': {
    forward: '{from} remixed {to}',
    inverse: '{to} was remixed by {from}',
  },
  'member of band|Artist|Artist': {
    forward: '{from} is (or was) a member of {to}',
    inverse: '{to} includes (or included) {from} as a member',
  },
  'main performer|Artist|Event': {
    forward: '{from} was the main performer at {to}',
    inverse: '{to} featured {from} as main performer',
  },
  'recorded at|Place|Recording': {
    forward: '{from} is where {to} was recorded',
    inverse: '{to} was recorded at {from}',
  },
  'recorded at|Event|Recording': {
    forward: '{from} is the source of the live recording {to}',
    inverse: '{to} was captured live at {from}',
  },
  'mixed at|Place|Recording': {
    forward: '{from} is where {to} was mixed',
    inverse: '{to} was mixed at {from}',
  },
  'held at|Event|Place': {
    forward: '{from} was held at {to}',
    inverse: '{to} hosted {from}',
  },
  'part of|Area|Area': {
    forward: '{from} is a subdivision of {to}',
    inverse: '{to} contains {from}',
  },
  'single from|ReleaseGroup|ReleaseGroup': {
    forward: '{from} is a single from {to}',
    inverse: '{to} produced the single {from}',
  },
  'support act|Artist|Event': {
    forward: '{from} was a support act at {to}',
    inverse: '{to} featured {from} as support',
  },
  'collaboration|Artist|Artist': {
    forward: '{from} has collaborated with {to}',
    inverse: '{to} has collaborated with {from}',
  },
  'teacher|Artist|Artist': {
    forward: '{from} was a teacher of {to}',
    inverse: '{to} was taught by {from}',
  },
  'remix|Recording|Recording': {
    forward: '{from} is a remix of {to}',
    inverse: '{to} was remixed as {from}',
  },
  'samples material|Recording|Recording': {
    forward: '{from} samples {to}',
    inverse: '{to} is sampled in {from}',
  },
  'parts|Work|Work': {
    forward: '{from} is a movement or part of {to}',
    inverse: '{to} contains {from}',
  },
  'adaptation|Work|Work': {
    forward: '{from} is an adaptation of {to}',
    inverse: '{to} was adapted as {from}',
  },
};

/**
 * Render a relationship sentence. If the rel/from/to triple is not in the
 * table, apply the LANGUAGE.md fallback rule (humanise + neutral phrasing).
 */
export function relSentence(
  rel: string,
  from: string,
  to: string,
  fromLabel: string,
  toLabel: string,
  direction: 'forward' | 'inverse',
): string {
  const key = `${rel}|${from}|${to}`;
  const phrase = REL_PHRASES[key];
  if (phrase) {
    const tmpl = direction === 'forward' ? phrase.forward : phrase.inverse;
    return tmpl.replace(/\{from\}/g, fromLabel).replace(/\{to\}/g, toLabel);
  }
  // Fallback rule.
  const human = humanise(rel);
  if (direction === 'forward') {
    return `${fromLabel} — ${human} — ${toLabel}`;
  }
  const article = /^[aeiou]/i.test(human) ? 'an' : 'a';
  return `${toLabel} has ${article} ${human} relationship with ${fromLabel}`;
}

// Title-case the rel for a subsection heading (Connections section).
export function relHeading(rel: string): string {
  return humanise(rel);
}
