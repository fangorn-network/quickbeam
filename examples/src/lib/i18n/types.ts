import type { IconName } from '../../components/Icon';

// A culturally-grounded quick-search pill. `q` is folded into the semantic query
// ("more like this feeling"), so labels and phrasing should read naturally in the
// locale's language and reflect what locals actually look for.
export interface Vibe {
  key: string;
  label: string;
  q: string;
  icon: IconName;
}

// Per-deployment locality identity. One SOND3R deployment is scoped to one place.
export interface Community {
  slug: string; // hofheim-am-taunus → sond3r.com/hofheim-am-taunus
  name: string; // Hofheim
  region: string; // Hessen
  regionAbbr: string; // HE
  tagline: string; // hero sub-headline (localized)
  blurb: string; // one supporting line (localized)
}

// The complete UI copy contract. Every locale MUST provide every key — TypeScript
// enforces completeness, so a translation can never ship half-done. Values that
// interpolate runtime data are functions.
export interface Strings {
  search: {
    placeholder: string;
    subtext: string;
    keyboardHint: string;
    clearAria: string;
    submit: string; // "Search" button / aria
    ariaByVibe: string; // input aria-label
  };
  filter: {
    label: string;
    allTypes: string;
  };
  cmdk: {
    ghost: string;
    groupSearch: string;
    groupTypes: string;
    groupRecent: string;
    groupResults: string;
    placeholder: string;
  };
  similar: {
    heading: string;
    subheading: (title: string) => string;
    empty: string;
    loading: string;
    scoreTooltip: string;
  };
  connections: {
    heading: string;
    loading: string;
    empty: string;
    emptyForEntry: string;
  };
  link: {
    softTooltip: (v: string) => string;
    hardTooltip: (title: string) => string;
    externalTooltip: string;
  };
  states: {
    loadingEntity: string;
    errorNotFound: string;
    errorNetwork: string;
    noResults: (q: string) => string;
    connectionError: string;
  };
  browse: {
    heading: string;
    recentHeading: string;
    recentEmpty: string;
  };
  // Landing hero + roadmap teaser.
  landing: {
    eyebrow: string; // brand line prefix, e.g. "SOND3R · {communityFull}" — set elsewhere
    discover: (name: string) => string; // "Discover Hofheim"
    claimPrompt: (name: string) => string; // "Run a business or host events in Hofheim?"
    claimSoon: string;
    contact: string; // text around the mailto
  };
  // Results page.
  results: {
    headlineNear: string; // "Closest to you"
    headlineQuery: (q: string) => string; // 'Matches for "…"'
    headlineVibe: string; // "Matching your vibe"
    headlineAround: (name: string) => string; // "Around Hofheim"
    vibesAria: string; // aria for the vibe-pill rail
    quickTonight: string;
    quickWeekend: string;
    quickEvents: string;
    mapTeaserTitle: string;
    mapTeaserSub: string;
    emptyVibe: string; // "Nothing here yet — try a different vibe."
    showMore: string;
    fallbackQuery: string; // used when no query text: noResults(fallbackQuery)
    everything: string; // "Everything" segment tab
    vibeFinderTitle: string;
    vibeFinderHint: string;
    whatsOnTitle: string;
    resetFilters: string;
    mapPreviewAria: string;
    countSpots: (n: number, more: boolean) => string; // "12 spots" / "12+ spots"
  };
  // Event-flavored entity rendering + contact links.
  event: {
    upcoming: string;
    past: string;
    cancelled: string;
    tickets: string;
    hostedBy: (organizer: string) => string;
    nearby: (coords: string) => string;
    pastGroup: string;
    upcomingOnly: string; // results toggle
    findMore: (organizer: string) => string; // tooltip
    findNear: (coords: string) => string; // tooltip
    website: string;
    map: string;
  };
}

// One deployable locale = language + locality + copy + quick-searches.
export interface LocaleProfile {
  id: string; // registry key, also the default VITE_LOCALE value
  lang: string; // BCP-47, drives <html lang> and Intl formatting
  community: Community;
  strings: Strings;
  vibes: Vibe[];
}
