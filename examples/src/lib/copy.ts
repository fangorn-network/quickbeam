// UI microcopy, from LANGUAGE.md §4.
export const COPY = {
  search: {
    placeholder: 'Search bars, events & places…',
    subtext: 'Search by name or meaning — try “live music this weekend” or “patio with food”',
    keyboardHint: 'Press / to focus',
    clearAria: 'Clear search',
  },
  filter: {
    label: 'Filter by type',
    allTypes: 'All types',
  },
  cmdk: {
    ghost: 'Search bars & events…',
    groupTypes: 'Browse',
    groupRecent: 'Recent',
    groupResults: 'Results',
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
    heading: 'Connections',
    loading: 'Loading connections…',
    empty: 'No connections recorded.',
    emptyForEntry: 'No connections recorded for this entry.',
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
} as const;
