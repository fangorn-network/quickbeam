// UI microcopy, from LANGUAGE.md §4.
export const COPY = {
  search: {
    placeholder: 'Search by name or keyword…',
    subtext: 'Keyword search over names, artists, tags & places — open an entry for meaning-based “similar” items',
    keyboardHint: 'Press / to focus',
    clearAria: 'Clear search',
  },
  filter: {
    label: 'Filter by type',
    allTypes: 'All types',
  },
  cmdk: {
    ghost: 'Cmd-K — Search anything…',
    groupTypes: 'Entity Types',
    groupRecent: 'Recent',
    groupResults: 'Search results',
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
    externalTooltip: 'View on MusicBrainz',
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
    heading: 'Browse by type',
    recentHeading: 'Recent activity',
    recentEmpty: 'No recent activity yet. Search or pick a type to begin.',
  },
} as const;
