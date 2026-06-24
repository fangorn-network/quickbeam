# Schema Browser — Language & Information Architecture Spec

**Version:** 1.0  
**Date:** 2026-06-20  
**Audience:** Frontend engineer, designer  
**Source data:** Qdrant collection `fangorn`, schema `fangorn.mb.creativecore.v3`

---

## 1. Entity Glossary

Each entityType maps to a display noun, article, and a one-sentence definition that drives the page lede template.

| `entityType` | Singular display noun | Plural display noun | Lede voice | Definition |
|---|---|---|---|---|
| `Artist` | Artist | Artists | "_{name}_ is a ..." | A person, group, or character who creates or performs music, ranging from solo musicians and bands to orchestras and fictional personas. |
| `Recording` | Recording | Recordings | "_{name}_ is a ..." | A specific audio (or video) capture of a musical performance, uniquely identified by its duration and ISRC codes. |
| `Release` | Release | Releases | "_{name}_ is a ..." | A physical or digital edition of an album, single, or EP — the concrete product that was distributed on a given date by a specific label. |
| `ReleaseGroup` | Album | Albums | "_{name}_ is a ..." | The abstract musical work that groups all editions of an album or single together (e.g., an LP and its deluxe reissue share one Album entry). |
| `Work` | Work | Works | "_{name}_ is a ..." | The underlying musical or lyrical composition — the song as written, independent of any particular performance or release. |
| `Event` | Event | Events | "_{name}_ was ..." | A dated live occurrence — a concert, festival, or broadcast — at which artists performed for an audience. |
| `Place` | Place | Places | "_{name}_ is a ..." | A physical location associated with music-making: a recording studio, concert hall, venue, or other facility. |
| `Area` | Area | Areas | "_{name}_ is a ..." | A geographic region — country, city, or subdivision — that contextualises where artists, events, and recordings originate. |
| `Instrument` | Instrument | Instruments | "_{name}_ is a ..." | A musical instrument or instrument family catalogued in MusicBrainz, including its lineage and variants. |

### Lede template

Compose the first sentence from `text` if present; otherwise fall back to:

```
{SingularNoun} · {entityType-specific subtitle line}
[text field value — used verbatim as the opening paragraph]
```

If `text` is absent: render only the subtitle line and suppress the paragraph element entirely (do not show a blank paragraph).

---

## 2. Field Label Map

`title` is always rendered as the page heading (H1); it is never shown as a labelled field row.

### Universal fields (present on all or most entity types)

| Field key | Human label | Type | Display note |
|---|---|---|---|
| `title` | _(page heading — not a labelled row)_ | string | Render as H1. |
| `text` | _(page lede — not a labelled row)_ | string | Render as the opening paragraph below the H1/subtitle. Suppress element if absent. |
| `mbid` | MusicBrainz ID | string | Render as an external link: `View on MusicBrainz ↗`. URL pattern per entity type — see §5. Show only the UUID in a monospace tooltip on hover; do not print it inline. |
| `entityType` | _(used for breadcrumb and badge only — not a labelled row)_ | string | Drives the entity-type badge and breadcrumb. Never display the raw camelCase value. |
| `schemaVersion` | _(internal — never display)_ | number | Suppress entirely. |
| `tags` | Tags | string | Comma-split and render as pill badges. If the string is empty or absent, suppress the row. |
| `disambiguation` | Note | string | Render in parentheses below the subtitle, e.g. "(not to be confused with the Californian band)". Suppress if absent. |
| `beginYear` | Active from | string | Plain year. If `endYear` is also present, merge into one row: "Active · 1982–2004". |
| `endYear` | Active until | string | See `beginYear`. If `beginYear` is absent and `endYear` is present: "Dissolved · {year}". |
| `rating` | Community rating | number | Display as a numeric score out of 5 (e.g. "4.2 / 5"). Suppress if 0 or absent. |
| `area` | Origin / Location | string | Plain string. If the value matches a known Area entity name, render as a search link (see §4 on link affordance). |

### Entity-specific fields

| Field key | Entity types | Human label | Type | Display note |
|---|---|---|---|---|
| `artistType` | Artist | Type | string | Values from MusicBrainz: Person, Group, Orchestra, Choir, Character, Other. Render verbatim. |
| `gender` | Artist | Gender | string | Render verbatim. Suppress if absent. |
| `sortName` | Artist | Sort name | string | Show in a small secondary line below the H1 only when it differs from `title`. |
| `byArtist` | Recording, Release, ReleaseGroup | By | string | Render as a search link to find the artist by name (see §4). Label: "By". |
| `durationMs` | Recording | Length | number | Format as `m:ss` (e.g. 243000 → "4:03"). Suppress if 0 or absent. |
| `isrcCodes` | Recording | ISRC | string | Comma-split; render each code in monospace. Suppress if absent. |
| `video` | Recording | Video recording | boolean | Show "Yes" only when `true`. Suppress row entirely when `false` or absent. |
| `datePublished` | Release, Event | Date | string | Render as-is (MusicBrainz partial dates are already human-readable, e.g. "2003-04-15"). |
| `labelName` | Release | Label | string | Plain string. |
| `status` | Release | Status | string | Values: Official, Promotion, Bootleg, Pseudo-Release. Render verbatim. |
| `barcode` | Release | Barcode | string | Render in monospace. Suppress if absent. |
| `primaryType` | ReleaseGroup | Format | string | Values: Album, Single, EP, Broadcast, Other. Render verbatim. |
| `iswcCodes` | Work | ISWC | string | Comma-split; render each code in monospace. Suppress if absent. |
| `workType` | Work | Work type | string | Values from MusicBrainz (Song, Symphony, Opera, etc.). Render verbatim. |
| `eventType` | Event | Event type | string | Values: Concert, Festival, Launch event, Award ceremony, etc. Render verbatim. |
| `time` | Event | Time | string | Render as-is. |
| `setlist` | Event | Setlist | string | Render as a prose block or line-split list. Suppress if absent. |
| `cancelled` | Event | Cancelled | boolean | Show a prominent "CANCELLED" badge when `true`. Suppress row when `false` or absent. |
| `placeType` | Place | Venue type | string | Values: Studio, Venue, Stadium, Festival grounds, etc. Render verbatim. |
| `address` | Place | Address | string | Render as plain text. |
| `coordinates` | Place | Location | string | Format as "lat, long" and optionally link to a map provider. Suppress if absent. |
| `areaType` | Area | Area type | string | Values: Country, Subdivision, City, District, Island, etc. Render verbatim. |
| `instrumentType` | Instrument | Instrument type | string | Values: Wind instrument, String instrument, Percussion, etc. Render verbatim. |
| `description` | Instrument | Description | string | Render as body text below the lede (Instrument only). |

---

## 3. Relationship Phrasing

All relationships are directional. The table below covers the ~25 highest-frequency rels. For the long tail, apply the **fallback rule** described after the table.

### Core relationship table

| `rel` (raw) | `from` → `to` | Forward sentence (reading from the `from` entity's page) | Inverse sentence (reading from the `to` entity's page) |
|---|---|---|---|
| `byArtist` | Recording → Artist | "{Recording} was recorded by {Artist}" | "{Artist} is the credited artist on {Recording}" |
| `byArtist` | Release → Artist | "{Release} was released by {Artist}" | "{Artist} released {Release}" |
| `byArtist` | ReleaseGroup → Artist | "{Album} was released by {Artist}" | "{Artist} released the album {Album}" |
| `hasTrack` | Release → Recording | "{Release} includes the track {Recording}" | "{Recording} appears on {Release}" |
| `hasRelease` | ReleaseGroup → Release | "{Album} has the edition {Release}" | "{Release} is an edition of the album {Album}" |
| `performance` | Recording → Work | "{Recording} is a performance of {Work}" | "{Work} was performed in {Recording}" |
| `composer` | Artist → Work | "{Artist} composed {Work}" | "{Work} was composed by {Artist}" |
| `writer` | Artist → Work | "{Artist} wrote {Work}" | "{Work} was written by {Artist}" |
| `lyricist` | Artist → Work | "{Artist} wrote the lyrics for {Work}" | "{Work}'s lyrics were written by {Artist}" |
| `arranger` | Artist → Work/Recording | "{Artist} arranged {Work/Recording}" | "{Work/Recording} was arranged by {Artist}" |
| `performer` | Artist → Recording | "{Artist} performed on {Recording}" | "{Recording} features {Artist} as performer" |
| `vocal` | Artist → Recording | "{Artist} provided vocals on {Recording}" | "{Recording} features vocals by {Artist}" |
| `instrument` | Artist → Recording | "{Artist} played on {Recording}" | "{Recording} features {Artist} on instrument" |
| `producer` | Artist → Recording | "{Artist} produced {Recording}" | "{Recording} was produced by {Artist}" |
| `engineer` | Artist → Recording | "{Artist} engineered {Recording}" | "{Recording} was engineered by {Artist}" |
| `mix` | Artist → Recording | "{Artist} mixed {Recording}" | "{Recording} was mixed by {Artist}" |
| `conductor` | Artist → Recording | "{Artist} conducted {Recording}" | "{Recording} was conducted by {Artist}" |
| `remixer` | Artist → Recording | "{Artist} remixed {Recording}" | "{Recording} was remixed by {Artist}" |
| `member of band` | Artist → Artist | "{Artist} is (or was) a member of {Artist}" | "{Artist} includes (or included) {Artist} as a member" |
| `main performer` | Artist → Event | "{Artist} was the main performer at {Event}" | "{Event} featured {Artist} as main performer" |
| `recorded at` | Place → Recording | "{Place} is where {Recording} was recorded" | "{Recording} was recorded at {Place}" |
| `recorded at` | Event → Recording | "{Event} is the source of the live recording {Recording}" | "{Recording} was captured live at {Event}" |
| `mixed at` | Place → Recording | "{Place} is where {Recording} was mixed" | "{Recording} was mixed at {Place}" |
| `held at` | Event → Place | "{Event} was held at {Place}" | "{Place} hosted {Event}" |
| `part of` | Area → Area | "{Area} is a subdivision of {Area}" | "{Area} contains {Area}" |
| `single from` | ReleaseGroup → ReleaseGroup | "{Album} is a single from {Album}" | "{Album} produced the single {Album}" |
| `support act` | Artist → Event | "{Artist} was a support act at {Event}" | "{Event} featured {Artist} as support" |
| `collaboration` | Artist → Artist | "{Artist} has collaborated with {Artist}" | "{Artist} has collaborated with {Artist}" |
| `teacher` | Artist → Artist | "{Artist} was a teacher of {Artist}" | "{Artist} was taught by {Artist}" |
| `founder` | Artist → Place/Artist | "{Artist} founded {Place/Artist}" | "{Place/Artist} was founded by {Artist}" |
| `remix` | Recording → Recording | "{Recording} is a remix of {Recording}" | "{Recording} was remixed as {Recording}" |
| `samples material` | Recording → Recording | "{Recording} samples {Recording}" | "{Recording} is sampled in {Recording}" |
| `parts` | Work → Work | "{Work} is a movement or part of {Work}" | "{Work} contains {Work}" |
| `adaptation` | Work → Work | "{Work} is an adaptation of {Work}" | "{Work} was adapted as {Work}" |

### Fallback rule for the long tail

For any `rel` string not in the table above, generate display text as follows:

1. **Humanise the raw string:** replace hyphens and underscores with spaces; title-case each word.  
   `"instrument-arranger"` → `"Instrument Arranger"` / `"vocal arranger"` → `"Vocal Arranger"`

2. **Forward (from `A`'s page, linking to `B`):**  
   `"{A} — {HumanisedRel} — {B}"` using an em dash separator and a neutral voice.  
   Example: "Miles Davis — Instrument Arranger — Kind of Blue"

3. **Inverse (from `B`'s page, linking to `A`):**  
   `"{B} has a {HumanisedRel} relationship with {A}"`  
   Example: "Kind of Blue has an Instrument Arranger relationship with Miles Davis"

4. **Never show the raw camelCase or raw lowercase multi-word rel string directly to the user.**

---

## 4. Navigation & UI Copy

### Global search bar

| Context | Copy |
|---|---|
| Placeholder text | `Search artists, albums, recordings, places…` |
| Subtext below bar | `Searches by name and meaning — try "jazz piano trios from Chicago"` |
| Keyboard hint | `Press / to focus` |
| Clear button aria-label | `Clear search` |

### Entity-type filter (alongside search)

| Context | Copy |
|---|---|
| Filter label | `Filter by type` |
| All-types option | `All types` |
| Individual options | Use plural display nouns from §1: "Artists", "Albums", "Recordings", "Releases", "Works", "Events", "Places", "Areas", "Instruments" |
| Active filter badge | `{PluralNoun} only · ×` |

### Breadcrumbs

Pattern: `Browse › {PluralNoun} › {entity title}`

| Page | Breadcrumb |
|---|---|
| Home / landing | `Browse` |
| Entity type listing | `Browse › Artists` |
| Entity detail page | `Browse › Artists › Miles Davis` |
| Search results | `Browse › Search results for "{query}"` |

### Semantic neighbors section (vector-similarity results)

This section surfaces Qdrant nearest-neighbor results — not hard graph edges. The copy must be honest about this.

| Element | Copy |
|---|---|
| Section heading | `Similar entries` |
| Section subheading | `These entries are semantically related to {entity title} — selected by meaning, not a direct link.` |
| Empty state | `No similar entries found.` |
| Loading | `Finding similar entries…` |
| Score badge tooltip | `Similarity score (higher = closer match)` |

### Relationship sections (hard graph edges)

| Element | Copy |
|---|---|
| Section heading | `Connections` |
| Subsection heading | Use the humanised rel string in title case, e.g. `Members`, `Tracks`, `Composers` |
| Loading | `Loading connections…` |
| Empty state | `No connections recorded.` |

### Link affordance — "follow this link runs a search"

Several fields (`byArtist`, `area`, etc.) hold a name string, not a hard ID reference. Clicking them runs a search. Make this unambiguous:

- Render such links with a search icon (magnifying glass) before the text, not an arrow.
- Tooltip on hover: `Search for "{value}"` — e.g. `Search for "John Coltrane"`
- Do **not** use the same underline style as hard entity links.
- Hard entity links (when an ID is known) use a standard underline and navigate directly; these show a `→` arrow icon.

Copy distinction summary:

| Link type | Icon | Tooltip | Behaviour |
|---|---|---|---|
| Hard entity link (ID known) | `→` | `View {entity title}` | Direct navigation to entity page |
| Soft search link (name string only) | `⌕` | `Search for "{value}"` | Triggers a name search; lands on results page |
| External MusicBrainz link | `↗` | `View on MusicBrainz` | Opens musicbrainz.org in a new tab |

### Empty / loading / error / no-results microcopy

| State | Location | Copy |
|---|---|---|
| Loading — entity page | Page body | `Loading…` (no filler skeleton text) |
| Loading — connections | Connections section | `Loading connections…` |
| Loading — similar | Similar section | `Finding similar entries…` |
| Error — entity not found | Page body | `This entry could not be found. It may have been removed or the link is outdated.` |
| Error — network / server | Page body | `Something went wrong. Please try refreshing the page.` |
| No results — search | Results list | `No results for "{query}". Try different words or remove the type filter.` |
| No results — connections | Connections section | `No connections recorded for this entry.` |
| No results — similar | Similar section | `No similar entries found.` |
| Empty field value | Any field row | Suppress the row entirely — never show "N/A" or "—" for missing data. |
| Cancelled event | Event page header | `CANCELLED` badge in red, directly below the H1. |

---

## 5. External Linking — MusicBrainz URL Patterns

Base URL: `https://musicbrainz.org`

| `entityType` | URL pattern | Example |
|---|---|---|
| `Artist` | `https://musicbrainz.org/artist/{mbid}` | `.../artist/65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab` |
| `Recording` | `https://musicbrainz.org/recording/{mbid}` | `.../recording/5465ca86-3881-4349-81b2-6efbd3a59451` |
| `Release` | `https://musicbrainz.org/release/{mbid}` | `.../release/76df3287-6cda-33eb-8e9a-044b5e15ffdd` |
| `ReleaseGroup` | `https://musicbrainz.org/release-group/{mbid}` | `.../release-group/70664514-3e47-3e51-8c88-0a23bbe8e3ee` |
| `Work` | `https://musicbrainz.org/work/{mbid}` | `.../work/b1df2cf3-69ab-4b3c-8b1c-a7f9e39fa5b3` |
| `Event` | `https://musicbrainz.org/event/{mbid}` | `.../event/ebe7f927-b573-4470-9d44-2f87f5a3e09e` |
| `Place` | `https://musicbrainz.org/place/{mbid}` | `.../place/4352063b-a833-421b-a420-e7fb295dece0` |
| `Area` | `https://musicbrainz.org/area/{mbid}` | `.../area/489ce91b-6658-3307-9877-795b68554c98` |
| `Instrument` | `https://musicbrainz.org/instrument/{mbid}` | `.../instrument/3347f963-edd5-4578-9a69-17543a758d3c` |

Note: `ReleaseGroup` maps to the hyphenated path segment `release-group` (not `releasegroup`). All other entityType values map directly to their lowercase form.

### TypeScript constants hint

```ts
// Copy-paste starter for the engineer
export const MB_URL_SEGMENT: Record<string, string> = {
  Artist: 'artist',
  Recording: 'recording',
  Release: 'release',
  ReleaseGroup: 'release-group',
  Work: 'work',
  Event: 'event',
  Place: 'place',
  Area: 'area',
  Instrument: 'instrument',
};

export const mbUrl = (entityType: string, mbid: string) =>
  `https://musicbrainz.org/${MB_URL_SEGMENT[entityType]}/${mbid}`;
```

---

## 6. Appendix — Field-to-entity Matrix

Quick reference for which fields appear on which entity types.

| Field | Area | Artist | Event | Instrument | Place | Recording | Release | ReleaseGroup | Work |
|---|---|---|---|---|---|---|---|---|---|
| `title` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `text` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `mbid` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `tags` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ |
| `disambiguation` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `beginYear` | ✓ | ✓ | — | — | ✓ | — | — | — | — |
| `endYear` | ✓ | ✓ | — | — | ✓ | — | — | — | — |
| `rating` | — | ✓ | — | — | — | ✓ | — | ✓ | — |
| `area` | — | ✓ | — | — | ✓ | — | — | — | — |
| `artistType` | — | ✓ | — | — | — | — | — | — | — |
| `gender` | — | ✓ | — | — | — | — | — | — | — |
| `sortName` | — | ✓ | — | — | — | — | — | — | — |
| `byArtist` | — | — | — | — | — | ✓ | ✓ | ✓ | — |
| `durationMs` | — | — | — | — | — | ✓ | — | — | — |
| `isrcCodes` | — | — | — | — | — | ✓ | — | — | — |
| `video` | — | — | — | — | — | ✓ | — | — | — |
| `datePublished` | — | — | ✓ | — | — | — | ✓ | — | — |
| `labelName` | — | — | — | — | — | — | ✓ | — | — |
| `status` | — | — | — | — | — | — | ✓ | — | — |
| `barcode` | — | — | — | — | — | — | ✓ | — | — |
| `primaryType` | — | — | — | — | — | — | — | ✓ | — |
| `iswcCodes` | — | — | — | — | — | — | — | — | ✓ |
| `workType` | — | — | — | — | — | — | — | — | ✓ |
| `eventType` | — | — | ✓ | — | — | — | — | — | — |
| `time` | — | — | ✓ | — | — | — | — | — | — |
| `setlist` | — | — | ✓ | — | — | — | — | — | — |
| `cancelled` | — | — | ✓ | — | — | — | — | — | — |
| `placeType` | — | — | — | — | ✓ | — | — | — | — |
| `address` | — | — | — | — | ✓ | — | — | — | — |
| `coordinates` | — | — | — | — | ✓ | — | — | — | — |
| `areaType` | ✓ | — | — | — | — | — | — | — | — |
| `instrumentType` | — | — | — | ✓ | — | — | — | — | — |
| `description` | — | — | — | ✓ | — | — | — | — | — |

---

_End of spec. Questions to the linguistic/IA author: driemworks@idealabs.network_
