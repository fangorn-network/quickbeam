import type { LocaleProfile } from './types';

// Deutsch · Hofheim am Taunus, Hessen — die deutsche SOND3R-Instanz. Tonfall: warm
// und persönlich (Duzen). Ein lokales Verzeichnis ("Branchenbuch") aus Orten +
// Events; die Schnellsuche-Kategorien decken das ganze Verzeichnis ab, nicht nur Lokale.
export const deHofheim: LocaleProfile = {
  id: 'de-hofheim',
  lang: 'de-DE',
  community: {
    slug: 'hofheim-am-taunus',
    name: 'Hofheim',
    region: 'Hessen',
    regionAbbr: 'HE',
    tagline:
      'Orte, Events & das Leben im Taunus. Suche nach Bedeutung, Stimmung, Ort – nach allem.',
    blurb: 'Ein lebendiger Wegweiser durch Hofheim am Taunus und Umgebung, durchsuchbar gemacht.',
  },
  strings: {
    search: {
      placeholder: 'Finde deine Stimmung in Hofheim … z. B. gemütliche Terrasse',
      subtext:
        'Such nach Gefühl, nicht nur nach Namen. Probier „Live-Musik am Abend“ oder „ruhige Terrasse für einen langen Nachmittag“',
      keyboardHint: 'Drücke /, um zu suchen',
      clearAria: 'Suche zurücksetzen',
      submit: 'Suchen',
      ariaByVibe: 'Nach Stimmung suchen',
    },
    filter: {
      label: 'Nach Typ filtern',
      allTypes: 'Alle Typen',
    },
    cmdk: {
      ghost: 'Orte & Events suchen …',
      groupSearch: 'Suche',
      groupTypes: 'Stöbern',
      groupRecent: 'Zuletzt',
      groupResults: 'Beste Treffer',
      placeholder: 'Suchen oder zu einem Typ springen …',
    },
    similar: {
      heading: 'Ähnliche Einträge',
      subheading: (title: string) =>
        `Diese Einträge sind ${title} inhaltlich verwandt – nach Bedeutung ausgewählt, nicht direkt verlinkt.`,
      empty: 'Keine ähnlichen Einträge gefunden.',
      loading: 'Suche ähnliche Einträge …',
      scoreTooltip: 'Ähnlichkeitswert (höher = passender)',
    },
    connections: {
      heading: 'Verbindungen',
      loading: 'Verbindungen werden geladen …',
      empty: 'Keine Verbindungen hinterlegt.',
      emptyForEntry: 'Für diesen Eintrag sind keine Verbindungen hinterlegt.',
    },
    link: {
      softTooltip: (v: string) => `Nach „${v}“ suchen`,
      hardTooltip: (title: string) => `${title} ansehen`,
      externalTooltip: 'Quelle ansehen',
    },
    states: {
      loadingEntity: 'Wird geladen …',
      errorNotFound:
        'Dieser Eintrag wurde nicht gefunden. Vielleicht wurde er entfernt oder der Link ist veraltet.',
      errorNetwork: 'Etwas ist schiefgelaufen. Bitte lade die Seite neu.',
      noResults: (q: string) =>
        `Keine Treffer für „${q}“. Versuch andere Wörter oder entferne den Typ-Filter.`,
      connectionError: 'Verbindungsfehler – neuer Versuch …',
    },
    browse: {
      heading: 'Entdecken',
      recentHeading: 'Zuletzt angesehen',
      recentEmpty: 'Noch nichts – such etwas oder wähle eine Kategorie.',
    },
    landing: {
      eyebrow: 'SOND3R',
      discover: (name: string) => `${name} entdecken`,
      claimPrompt: (name: string) => `Betreibst du ein Geschäft oder veranstaltest du Events in ${name}?`,
      claimSoon: 'Profil übernehmen & Events selbst einstellen – bald verfügbar.',
      contact: 'Melde dich unter',
    },
    results: {
      headlineNear: 'Am nächsten bei dir',
      headlineQuery: (q: string) => `Treffer für „${q}“`,
      headlineVibe: 'Passend zu deiner Stimmung',
      headlineAround: (name: string) => `Rund um ${name}`,
      vibesAria: 'Nach Stimmung suchen',
      quickTonight: 'Heute Abend',
      quickWeekend: 'Dieses Wochenende',
      quickEvents: 'Ausgewählte Events',
      mapTeaserTitle: 'Karte erkunden',
      mapTeaserSub: 'Markierungen für jeden Ort & jedes Event – bald verfügbar.',
      emptyVibe: 'Hier ist noch nichts – probier eine andere Stimmung.',
      showMore: 'Mehr Orte anzeigen',
      fallbackQuery: 'diese Stimmung',
      everything: 'Alles',
      vibeFinderTitle: 'Stimmung finden',
      vibeFinderHint: 'Tipp ein Gefühl an, um die Suche zu lenken.',
      whatsOnTitle: 'Was läuft',
      resetFilters: 'Filter zurücksetzen',
      mapPreviewAria: 'Kartenvorschau',
      countSpots: (n: number, more: boolean) =>
        `${n}${more ? '+' : ''} ${n === 1 ? 'Ort' : 'Orte'}`,
    },
    event: {
      upcoming: 'Demnächst',
      past: 'Vergangen',
      cancelled: 'Abgesagt',
      tickets: 'Tickets ↗',
      hostedBy: (organizer: string) => `Veranstaltet von ${organizer}`,
      nearby: (coords: string) => `◎ In der Nähe (${coords})`,
      pastGroup: 'Vergangen',
      upcomingOnly: 'Nur kommende Events',
      findMore: (organizer: string) => `Mehr von ${organizer} finden`,
      findNear: (coords: string) => `Orte in der Nähe von ${coords} finden`,
      website: 'Website ↗',
      map: 'Karte ↗',
    },
  },
  vibes: [
    { key: 'essen', label: 'Essen & Trinken', q: 'Restaurant Lokal Gaststätte essen trinken', icon: 'glass' },
    { key: 'kaffee', label: 'Café & Kuchen', q: 'Café Kaffee Kuchen Bäckerei gemütlich', icon: 'sparkle' },
    { key: 'geschaefte', label: 'Geschäfte', q: 'Geschäft Laden einkaufen Boutique Markt', icon: 'star' },
    { key: 'dienste', label: 'Dienstleistungen', q: 'Dienstleistung Friseur Werkstatt Bank Apotheke', icon: 'compass' },
    { key: 'nachtleben', label: 'Nachtleben', q: 'Bar Kneipe Pub Nachtleben spät', icon: 'moon' },
    { key: 'draussen', label: 'Natur & Draußen', q: 'Park Natur draußen Spaziergang Taunus', icon: 'leaf' },
    { key: 'lokal', label: 'Lokale Lieblinge', q: 'beliebtes lokales Lieblingslokal Geheimtipp', icon: 'fish' },
    { key: 'events', label: 'Events & Musik', q: 'Live-Musik Veranstaltung Event Konzert', icon: 'music' },
  ],
};
