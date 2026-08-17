import type { LocaleProfile } from './types';

// English · Oak Cliff, Dallas, Texas — the big, walkable district southwest of the
// Trinity: Bishop Arts, the Jefferson Boulevard corridor, Tyler/Davis, Kessler and
// Winnetka Heights. Taquerías, cocktail bars, vintage shops, galleries, theaters &
// parks. Copy keeps an Oak Cliff flavor while the quick-search vibes span the whole
// directory. Data: OSM (places) + Eventbrite (events); no Google Places.
export const enOakCliff: LocaleProfile = {
  id: 'en-oak-cliff',
  lang: 'en-US',
  community: {
    slug: 'oak-cliff-dallas',
    name: 'Oak Cliff',
    region: 'Texas',
    regionAbbr: 'TX',
    tagline:
      'Taquerías, cocktail bars, vintage shops, galleries & live music across Oak Cliff. Search by meaning, vibe, location, anything.',
    blurb: 'A living guide to Oak Cliff in Dallas — Bishop Arts to Jefferson Blvd — made searchable.',
    center: [-96.84, 32.73], // greater Oak Cliff, Dallas
  },
  strings: {
    search: {
      placeholder: 'Find your Oak Cliff vibe… e.g., patio tacos & a margarita',
      subtext:
        'Search by feel, not just name. Try “live music with a cocktail” or “vintage shopping then coffee”',
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
      mapTeaserSub: 'Pins for every taquería, bar, shop, gallery & landmark — open the map.',
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
    { key: 'tacos', label: 'Tacos & Mexican', q: 'taqueria tacos mexican tex-mex enchiladas margarita', icon: 'glass' },
    { key: 'coffee', label: 'Coffee & cafés', q: 'coffee shop cafe espresso matcha bakery', icon: 'sparkle' },
    { key: 'cocktails', label: 'Cocktail bars', q: 'cocktail bar speakeasy wine cider patio drinks', icon: 'moon' },
    { key: 'vintage', label: 'Vintage & boutiques', q: 'vintage thrift boutique antiques records shopping', icon: 'star' },
    { key: 'art', label: 'Art & galleries', q: 'art gallery mural studio craft handmade theater', icon: 'compass' },
    { key: 'brunch', label: 'Brunch', q: 'brunch breakfast diner patio weekend', icon: 'leaf' },
    { key: 'local', label: 'Local favorites', q: 'beloved local favorite hidden gem oak cliff', icon: 'fish' },
    { key: 'events', label: 'Events & music', q: 'live music event show happening festival', icon: 'music' },
  ],
};
