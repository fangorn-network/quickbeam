import type { LocaleProfile } from './types';

// English · Eagle River, Wisconsin — the original SOND3R deployment. A local
// directory ("yellowpages") of places + events; copy keeps a Northwoods flavor
// while the quick-search categories span the whole directory, not just bars.
export const enEagleRiver: LocaleProfile = {
  id: 'en-eagle-river',
  lang: 'en-US',
  community: {
    slug: 'eagle-river-wisconsin',
    name: 'Eagle River',
    region: 'Wisconsin',
    regionAbbr: 'WI',
    tagline:
      'Local places, events & everyday life in the Northwoods. Search by meaning, vibe, location, anything.',
    blurb: 'A living guide to Eagle River, WI and its environs, made searchable.',
  },
  strings: {
    search: {
      placeholder: 'Find your Eagle River vibe… e.g., cozy lakeside deck',
      subtext:
        'Search by feel, not just name. Try “live music on the water” or “quiet patio for a slow afternoon”',
      keyboardHint: 'Press / to focus',
      clearAria: 'Clear search',
      submit: 'Search',
      ariaByVibe: 'Search by vibe',
    },
    filter: {
      label: 'Filter by type',
      allTypes: 'All types',
    },
    cmdk: {
      ghost: 'Search places & events…',
      groupSearch: 'Search',
      groupTypes: 'Browse',
      groupRecent: 'Recent',
      groupResults: 'Top matches',
      placeholder: 'Search or jump to a type…',
    },
    similar: {
      heading: 'Similar entries',
      subheading: (title: string) =>
        `These entries are semantically related to ${title} — selected by meaning, not a direct link.`,
      empty: 'No similar entries found.',
      loading: 'Finding similar entries…',
      scoreTooltip: 'Similarity score (higher = closer match)',
    },
    connections: {
      heading: 'Related',
      loading: 'Loading…',
      empty: 'Nothing related yet.',
      emptyForEntry: 'Nothing related yet.',
    },
    link: {
      softTooltip: (v: string) => `Search for "${v}"`,
      hardTooltip: (title: string) => `View ${title}`,
      externalTooltip: 'View source',
    },
    states: {
      loadingEntity: 'Loading…',
      errorNotFound:
        'This entry could not be found. It may have been removed or the link is outdated.',
      errorNetwork: 'Something went wrong. Please try refreshing the page.',
      noResults: (q: string) =>
        `No results for "${q}". Try different words or remove the type filter.`,
      connectionError: 'Connection error — retrying…',
    },
    browse: {
      heading: 'Explore',
      recentHeading: 'Recently viewed',
      recentEmpty: 'Nothing yet — search or pick a category to begin.',
    },
    landing: {
      eyebrow: 'SOND3R',
      discover: (name: string) => `Discover ${name}`,
      claimPrompt: (name: string) => `Run a business or host events in ${name}?`,
      claimSoon: 'Claiming your profile & self-serve events are coming soon.',
      contact: 'Get in touch at',
    },
    results: {
      headlineNear: 'Closest to you',
      headlineQuery: (q: string) => `Matches for “${q}”`,
      headlineVibe: 'Matching your vibe',
      headlineAround: (name: string) => `Around ${name}`,
      vibesAria: 'Search by vibe',
      quickTonight: "Tonight's picks",
      quickWeekend: 'This weekend',
      quickEvents: 'Featured events',
      mapTeaserTitle: 'Explore the map',
      mapTeaserSub: 'Pins for every place, trail, lake & landmark — open the map.',
      emptyVibe: 'Nothing here yet — try a different vibe.',
      showMore: 'Show more places',
      fallbackQuery: 'that vibe',
      everything: 'Everything',
      vibeFinderTitle: 'Vibe finder',
      vibeFinderHint: 'Tap a feeling to steer the search.',
      whatsOnTitle: "What's on",
      resetFilters: 'Reset filters',
      mapPreviewAria: 'Map preview',
      countSpots: (n: number, more: boolean) =>
        `${n}${more ? '+' : ''} ${n === 1 ? 'place' : 'places'}`,
    },
    event: {
      upcoming: 'Upcoming',
      past: 'Past event',
      cancelled: 'Cancelled',
      tickets: 'Tickets ↗',
      hostedBy: (organizer: string) => `Hosted by ${organizer}`,
      nearby: (coords: string) => `◎ Nearby (${coords})`,
      pastGroup: 'Past',
      upcomingOnly: 'Upcoming events only',
      findMore: (organizer: string) => `Find more from ${organizer}`,
      findNear: (coords: string) => `Find places near ${coords}`,
      website: 'Website ↗',
      map: 'Map ↗',
    },
  },
  vibes: [
    { key: 'eatdrink', label: 'Eat & drink', q: 'restaurant bar cafe diner food dining', icon: 'glass' },
    { key: 'coffee', label: 'Coffee & cafés', q: 'coffee shop cafe espresso bakery', icon: 'sparkle' },
    { key: 'shops', label: 'Shops', q: 'store shop retail boutique market', icon: 'star' },
    { key: 'services', label: 'Services', q: 'services repair salon bank hardware', icon: 'compass' },
    { key: 'nightlife', label: 'Nightlife', q: 'bar pub tavern night club late night', icon: 'moon' },
    { key: 'outdoors', label: 'Outdoors', q: 'park trail lake outdoor recreation', icon: 'leaf' },
    { key: 'local', label: 'Local favorites', q: 'beloved local favorite hidden gem', icon: 'fish' },
    { key: 'events', label: 'Events & music', q: 'live music event show happening', icon: 'music' },
  ],
};
