// Locale registry + active-profile selection.
//
// One SOND3R build serves one community in one language. `VITE_LOCALE` picks the
// profile (id below); per-field `VITE_COMMUNITY_*` env vars still layer on top so a
// deployment can tweak the locality without authoring a whole new profile. Add a
// new community/language by dropping a LocaleProfile file here and registering it.
import type { Community, LocaleProfile, Strings, Vibe } from './types';
// import { enEagleRiver } from './en-eagle-river';
import { enJackson } from './en-jackson';
import { deHofheim } from './de-hofheim';
import { enOakCliff } from './en-oak-cliff';

export type { Community, LocaleProfile, Strings, Vibe } from './types';

const env = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};

export const LOCALES: Record<string, LocaleProfile> = {
  [enJackson.id]: enJackson,
  [deHofheim.id]: deHofheim,
  [enOakCliff.id]: enOakCliff,
};

const DEFAULT_LOCALE = enJackson.id;
const requested = env.VITE_LOCALE ?? DEFAULT_LOCALE;
export const LOCALE: LocaleProfile = LOCALES[requested] ?? LOCALES[DEFAULT_LOCALE];

if (!LOCALES[requested]) {
  console.warn(
    `[i18n] VITE_LOCALE="${requested}" not found; falling back to "${DEFAULT_LOCALE}". ` +
      `Known: ${Object.keys(LOCALES).join(', ')}`,
  );
}

// BCP-47 language tag for <html lang> and Intl formatting.
export const LANG = LOCALE.lang;

// Active community, with per-field env overrides layered on top of the profile.
export const COMMUNITY: Community = {
  slug: env.VITE_COMMUNITY_SLUG ?? LOCALE.community.slug,
  name: env.VITE_COMMUNITY_NAME ?? LOCALE.community.name,
  region: env.VITE_COMMUNITY_REGION ?? LOCALE.community.region,
  regionAbbr: env.VITE_COMMUNITY_REGION_ABBR ?? LOCALE.community.regionAbbr,
  tagline: env.VITE_COMMUNITY_TAGLINE ?? LOCALE.community.tagline,
  blurb: env.VITE_COMMUNITY_BLURB ?? LOCALE.community.blurb,
};

// Active copy pack + quick-search vibes.
export const COPY: Strings = LOCALE.strings;
export const VIBES: Vibe[] = LOCALE.vibes;

// "Hofheim · HE" — compact label for the top-bar chip.
export const communityChip = `${COMMUNITY.name} · ${COMMUNITY.regionAbbr}`;
// "Hofheim, Hessen" — full label for the hero / document title.
export const communityFull = `${COMMUNITY.name}, ${COMMUNITY.region}`;
