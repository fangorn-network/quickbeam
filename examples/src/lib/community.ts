// Per-community branding. A SOND3R deployment is scoped to one community; the
// `slug` is the path it lives at (sond3r.com/<slug>) and, later, selects which
// baked snapshot is served. Overridable per deployment via VITE_COMMUNITY_* so a
// single build can be reused across communities.
const env = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};

export interface Community {
  slug: string; // eagle-river-wisconsin  → sond3r.com/eagle-river-wisconsin
  name: string; // Eagle River
  region: string; // Wisconsin
  regionAbbr: string; // WI
  tagline: string; // hero sub-headline
  blurb: string; // one line of supporting copy
}

export const COMMUNITY: Community = {
  slug: env.VITE_COMMUNITY_SLUG ?? 'eagle-river-wisconsin',
  name: env.VITE_COMMUNITY_NAME ?? 'Eagle River',
  region: env.VITE_COMMUNITY_REGION ?? 'Wisconsin',
  regionAbbr: env.VITE_COMMUNITY_REGION_ABBR ?? 'WI',
  tagline:
    env.VITE_COMMUNITY_TAGLINE ??
    'Bars, events & local life in the Northwoods. Search by meaning, vibe, location, anything.',
  blurb:
    env.VITE_COMMUNITY_BLURB ??
    'A living guide to Eagle River, WI and its environs, made searchable.',
};

// "Eagle River · WI" — the compact label for the top-bar chip.
export const communityChip = `${COMMUNITY.name} · ${COMMUNITY.regionAbbr}`;
// "Eagle River, Wisconsin" — the full label for the hero / document title.
export const communityFull = `${COMMUNITY.name}, ${COMMUNITY.region}`;
