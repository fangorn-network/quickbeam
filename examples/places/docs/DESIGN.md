# Schema Browser — Visual & Interaction Design Spec

> Version 1.0 · 2026-06-20 · Product Design Phase
> For: Frontend Engineer + Linguist teammates
> Stack target: React + Vite + plain CSS Modules (no heavy UI framework)

---

## 1. Design Principles

- **Information-dense, not cluttered.** Every pixel earns its place. Tables over cards; small type is fine when hierarchy is clear. Think a refined dark terminal — not a dashboard.
- **Graph-native navigation.** The browser makes the graph *feel* traversable. Every field value that could link somewhere is clickable. The back stack and breadcrumb tell you where you came from in the graph, not in the browser history.
- **Honest about data limits.** We don't fabricate edges. "Related" means semantic neighbors via vector search or field-match; we label the mechanism (e.g. "via byArtist" or "semantic"). No implicit magic.
- **Keyboard first.** Cmd-K opens everything. J/K navigate lists. Enter follows a link. Esc closes drawers. The mouse is optional.
- **Fast to scan, slow to lose.** Type badges, accent stripes, and icons make entity type legible in < 200ms. A user landing on any page should know what kind of thing they're looking at before they read a word.
- **Calm professionalism.** This is a research tool, not a product marketing page. No gradients on content, no animations > 150ms, no emoji in data surfaces.

---

## 2. Color + Type System

### CSS Custom Properties

```css
:root {
  /* — Surface — */
  --bg-base:       #0d0f12;   /* page background */
  --bg-surface:    #14171c;   /* card / panel */
  --bg-elevated:   #1c2028;   /* popover / drawer */
  --bg-hover:      #22272f;   /* row hover */
  --bg-active:     #272d38;   /* selected row */

  /* — Borders — */
  --border-subtle: #252a33;
  --border-strong: #363d4a;

  /* — Text — */
  --text-primary:  #e8eaf0;
  --text-secondary:#8b93a6;
  --text-muted:    #555f72;
  --text-link:     #7eb8f7;
  --text-link-hover:#aad2ff;

  /* — Accent: used for entity type identity (see §3) — */
  --accent-artist:      #a78bfa;  /* violet */
  --accent-recording:   #34d399;  /* emerald */
  --accent-release:     #60a5fa;  /* sky blue */
  --accent-releasegroup:#f472b6;  /* pink */
  --accent-work:        #fbbf24;  /* amber */
  --accent-place:       #fb923c;  /* orange */
  --accent-event:       #f87171;  /* red */
  --accent-area:        #38bdf8;  /* cyan */
  --accent-instrument:  #a3e635;  /* lime */

  /* — Semantic — */
  --color-success: #34d399;
  --color-warning: #fbbf24;
  --color-error:   #f87171;
  --color-info:    #60a5fa;

  /* — Typography — */
  --font-sans: 'Inter', 'Helvetica Neue', Arial, sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;

  /* — Type Scale (rem, base 16px) — */
  --text-xs:   0.6875rem;  /* 11px — labels, badges */
  --text-sm:   0.8125rem;  /* 13px — table body, secondary */
  --text-base: 0.9375rem;  /* 15px — body */
  --text-md:   1.0625rem;  /* 17px — section headers */
  --text-lg:   1.25rem;    /* 20px — page title */
  --text-xl:   1.625rem;   /* 26px — hero entity name */

  /* — Spacing Scale (4px base) — */
  --space-1:  0.25rem;   /*  4px */
  --space-2:  0.5rem;    /*  8px */
  --space-3:  0.75rem;   /* 12px */
  --space-4:  1rem;      /* 16px */
  --space-6:  1.5rem;    /* 24px */
  --space-8:  2rem;      /* 32px */
  --space-12: 3rem;      /* 48px */

  /* — Radius — */
  --radius-sm: 3px;
  --radius-md: 6px;
  --radius-lg: 10px;

  /* — Motion — */
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --duration-fast: 80ms;
  --duration-std:  140ms;
}
```

### Light Mode Override (optional, scoped to `[data-theme="light"]`)

```css
[data-theme="light"] {
  --bg-base:       #f4f6fa;
  --bg-surface:    #ffffff;
  --bg-elevated:   #f0f2f7;
  --bg-hover:      #e8ecf4;
  --border-subtle: #dde2ed;
  --border-strong: #c5ccd9;
  --text-primary:  #1a1e26;
  --text-secondary:#4a5568;
  --text-muted:    #8b95a6;
}
```

---

## 3. Per-Entity Type Visual Identity

| Entity Type   | Accent Token             | Hex       | Icon Idea                        | Left-rail letter badge |
|---------------|--------------------------|-----------|----------------------------------|------------------------|
| Artist        | `--accent-artist`        | `#a78bfa` | Person silhouette (mic optional) | **A**                  |
| Recording     | `--accent-recording`     | `#34d399` | Waveform / sound bar             | **R**                  |
| Release       | `--accent-release`       | `#60a5fa` | Vinyl / CD disc                  | **Re**                 |
| ReleaseGroup  | `--accent-releasegroup`  | `#f472b6` | Stacked discs                    | **RG**                 |
| Work          | `--accent-work`          | `#fbbf24` | Pen / manuscript                 | **W**                  |
| Place         | `--accent-place`         | `#fb923c` | Map pin                          | **Pl**                 |
| Event         | `--accent-event`         | `#f87171` | Calendar with bolt               | **Ev**                 |
| Area          | `--accent-area`          | `#38bdf8` | Globe / polygon outline          | **Ar**                 |
| Instrument    | `--accent-instrument`    | `#a3e635` | Music note / string              | **In**                 |

Each entity type uses its accent color in three places: (1) the left-edge stripe on EntityBadge; (2) the top border on the entity page header card; (3) the dot in the ResultCard type indicator. The accent is never used as a background fill on large areas — always as a 2-4px stripe or small swatch, preserving legibility.

---

## 4. Layout / Wireframes

### 4a. Global Search / Landing

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ▣ schema-browser                    [Cmd-K — Search anything...]    [⚙ ···] │  ← TopBar (48px)
├──────────────┬──────────────────────────────────────────────────────────────┤
│  LEFT RAIL   │                                                              │
│  (220px)     │          MAIN CONTENT (flex-1, centered max-w 760px)         │
│              │                                                              │
│ ▸ TYPES      │    ┌─────────────────────────────────────────────────────┐   │
│   ● All      │    │  ⌕  Search artists, recordings, places…             │   │
│   ○ Artist   │    └─────────────────────────────────────────────────────┘   │
│   ○ Recording│                                                              │
│   ○ Release  │    Browse by type                                            │
│   ○ Work     │    ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐         │
│   ○ Place    │    │ 🎤      │ │ ≋       │ │ 💿      │ │ 📌      │         │
│   ○ Event    │    │ Artist  │ │ Rec.    │ │ Release │ │ Place   │         │
│   ○ Area     │    │ — k pts │ │ — k pts │ │ — k pts │ │ — k pts │         │
│   ○ Inst.    │    └─────────┘ └─────────┘ └─────────┘ └─────────┘         │
│   ○ RG       │    ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐         │
│              │    │ 📅      │ │ ✏       │ │ 🌐      │ │ 🎸      │         │
│ ─────────── │    │ Event   │ │ Work    │ │ Area    │ │ Instr.  │         │
│ ▸ RECENT    │    └─────────┘ └─────────┘ └─────────┘ └─────────┘         │
│  Radiohead   │                                                              │
│  OK Computer │    Recent activity (last 10 visited)                         │
│  Abbey Road  │    ┌────────────────────────────────────────────────────┐    │
│  London      │    │ [RG] OK Computer · Radiohead · 1997                │    │
│              │    │ [A]  Radiohead · Artist · Oxford, England          │    │
│              │    │ [R]  Creep (1992) · Recording · 3:56               │    │
│              │    └────────────────────────────────────────────────────┘    │
└──────────────┴──────────────────────────────────────────────────────────────┘
```

### 4b. Entity Page (the "Article")

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ▣ schema-browser   [Cmd-K …]                                        [⚙ ···] │
├──────────────┬──────────────────────────────────────────────────────────────┤
│  LEFT RAIL   │ Breadcrumb: All > Artist > Radiohead                         │
│  (active:    ├──────────────────────────────────────────────────────────────┤
│   Artist ●) │ ┌─ ENTITY HEADER (accent top-border: violet) ──────────────┐ │
│              │ │ [A] Artist                     mbid: a74b…  [Copy] [↗MB] │ │
│              │ │                                                           │ │
│ ▸ TYPES      │ │  Radiohead                                                │ │
│   ● Artist   │ │  Oxford, England · Active 1985–present                   │ │
│   ○ Rec.     │ │  Tags: alternative rock · art rock · british             │ │
│   …          │ └───────────────────────────────────────────────────────────┘ │
│              │                                                              │
│ ─────────── │ ┌─ FIELDS ──────────────────────────────────────────────────┐ │
│ ▸ RECENT    │ │ Field            Value                         Type        │ │
│  Radiohead ← │ │ ─────────────── ───────────────────────────── ─────────── │ │
│  OK Computer │ │ title           Radiohead                      string      │ │
│  Creep       │ │ area            [↗ United Kingdom]             link        │ │
│              │ │ disambiguation  "Oxford band"                  string      │ │
│              │ │ artistType      Group                          string      │ │
│              │ │ beginYear       1985                           number      │ │
│              │ │ rating          4.8                            number      │ │
│              │ │ [▼ show raw JSON]                                          │ │
│              │ └───────────────────────────────────────────────────────────┘ │
│              │                                                              │
│              │ ┌─ RELATED (via field match) ───────────────────────────────┐ │
│              │ │ Recordings by this artist  [→ search byArtist=Radiohead]  │ │
│              │ │   ├ Creep · 1992 · 3:56                                   │ │
│              │ │   ├ Karma Police · 1997 · 4:23                            │ │
│              │ │   └ [View all 48 →]                                       │ │
│              │ │                                                            │ │
│              │ │ Member of / members  [via member of band edge vocab]      │ │
│              │ │   └ [Search triggered, no direct edges stored — see note] │ │
│              │ └───────────────────────────────────────────────────────────┘ │
│              │                                                              │
│              │ ┌─ SEMANTIC NEIGHBORS (via Qdrant recommend) ───────────────┐ │
│              │ │ Similarity basis: 256-d cosine · top 6                    │ │
│              │ │   [A] Portishead   [A] Thom Yorke   [A] Massive Attack    │ │
│              │ │   [A] PJ Harvey    [A] Björk         [A] Nick Cave        │ │
│              │ └───────────────────────────────────────────────────────────┘ │
└──────────────┴──────────────────────────────────────────────────────────────┘
```

Raw JSON drawer: slides up from bottom (300px tall, resizable), monospace, syntax-highlighted, collapsible. Triggered by "show raw JSON" toggle in Fields section.

### 4c. Results List / Browse-by-Type

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ▣ schema-browser   [Cmd-K …]                                        [⚙ ···] │
├──────────────┬──────────────────────────────────────────────────────────────┤
│  LEFT RAIL   │ Breadcrumb: All > Artist > "radiohead"                       │
│  (Artist ●) ├──────────────────────────────────────────────────────────────┤
│              │ Query: "radiohead"  ·  Type: Artist  ·  12 results           │
│ ▸ TYPES      │ ┌────────────────────────────────────────────────────────────┐│
│   ● Artist   │ │ [A] Radiohead                                    score 0.97││
│              │ │     Oxford, England · Group · Active 1985–present          ││
│              │ │     Tags: alternative rock · art rock                      ││
│              │ ├────────────────────────────────────────────────────────────┤│
│ FILTERS      │ │ [A] Colin Greenwood                              score 0.81││
│ ─────────── │ │     Member of Radiohead · Bassist                          ││
│ beginYear    │ ├────────────────────────────────────────────────────────────┤│
│ [1980]–[2000]│ │ [A] Ed O'Brien                                   score 0.78││
│              │ │     Member of Radiohead · Guitarist                        ││
│ rating ≥ [3] │ ├────────────────────────────────────────────────────────────┤│
│              │ │  … 9 more results                                          ││
│ ─────────── │ └────────────────────────────────────────────────────────────┘│
│ ▸ RECENT    │                                                              │
│  …           │ Search mechanism: full-text on title/byArtist + type filter  │
└──────────────┴──────────────────────────────────────────────────────────────┘
```

Filters panel in left rail shows numeric range sliders for number fields (rating, beginYear, endYear, durationMs) and tag chips for the selected type's most common tags. Filters are additive (AND). Results are paginated in groups of 20.

---

## 5. Key Interactions

### 5.1 Command Palette (Cmd-K)

- Opens a floating centered modal (max-w 560px, top 20% of viewport).
- Input is auto-focused. Debounced search at 200ms.
- Results grouped: "Entity Types" (jump to browse), "Recent" (visited pages), "Search results" (live Qdrant query).
- Keyboard: Up/Down to navigate groups and items; Enter to navigate; Esc to close.
- Each result row shows: EntityBadge (type + accent stripe) + primary label + secondary line.
- No mouse hover required — keyboard highlight tracks arrow keys.

### 5.2 Breadcrumb / Back Stack (Wiki History)

- Persisted in browser sessionStorage, max 20 entries.
- Displayed as a horizontal breadcrumb above the entity header: `All › Artist › Radiohead › OK Computer`.
- Each crumb is a clickable link that restores that page (not browser back — so opening in new tab preserves the current stack).
- Left rail "Recent" is a deduplicated, reverse-chronological view of the same stack.
- Back button (← in TopBar) pops the stack.

### 5.3 Hover Previews

- Hovering a field value that resolves to an entity (e.g. a byArtist link) shows a popover after 400ms delay.
- Popover (240px wide) contains: EntityBadge, primary title, 2-3 key fields, "Open →" link.
- Popover dismissed on mouse leave with 150ms grace period (so cursor can enter the popover).
- Triggered by: FieldTable link cells, RelatedRail items, SemanticNeighbor cards.
- Not triggered on result list rows (those are already expanded cards).

### 5.4 Loading States

- **Page skeleton:** Three stacked gray shimmer blocks (header shape + field table shape + related shape). Animate with a left-to-right shimmer sweep at 1.4s loop. No spinner.
- **Inline search (Cmd-K):** Show last results immediately, then replace when new results arrive. No blank flash.
- **Semantic neighbors:** Load independently (separate Qdrant call); show "Loading neighbors…" placeholder text until resolved; does not block page render.

### 5.5 Empty States

- **No results for search:** Centered text "No results for «query»" + "Try searching in All types" link + "Clear filters" button if filters are active.
- **Empty field:** Field row omitted from table (no "—" clutter); toggling "show all fields" reveals empty rows in muted style.
- **No semantic neighbors:** Section collapses with "No similar entities found" — not an error.

### 5.6 Error States

- **Qdrant unavailable:** TopBar gains a red left-border accent + "Connection error — retrying…" badge. Main content areas show a non-intrusive inline message, not a full-page error.
- **Point not found (bad UUID):** 404-style entity page with "Entity not found" heading and link back to search.

---

## 6. Component Inventory

These are the concrete reusable components the frontend engineer should build. Each is a CSS Module component. Acceptance criteria are listed inline.

### `EntityBadge`
**Props:** `type: EntityType, size?: 'sm'|'md'|'lg'`
**Renders:** A pill with: 2px left accent stripe in the type's color, type icon (SVG), type label abbreviation. Used everywhere a type needs to be identified at a glance.
**Sizes:** sm = 18px tall (table cells, crumbs); md = 22px (result cards); lg = 28px (page header).

### `TopBar`
**Props:** `onCmdK: () => void, onBack: () => void, canGoBack: boolean`
**Renders:** Fixed 48px bar. Left: logo/wordmark. Center: ghost search button showing "Cmd-K — Search anything…" (clicking triggers command palette). Right: settings gear.

### `LeftRail`
**Props:** `activeType?: EntityType, onTypeSelect: (t: EntityType | null) => void, recentPages: PageRef[]`
**Renders:** 220px fixed sidebar. Two sections: TYPES (radio-style type filter, counts from a stats endpoint) and RECENT (last 10 visited pages as compact links). Collapses to 48px icon rail on narrow viewports (< 900px).

### `SearchBar`
**Props:** `initialValue?: string, onSearch: (q: string, type?: EntityType) => void`
**Renders:** Prominent input used on the landing page only (not the TopBar ghost button). Includes entity type selector dropdown inline.

### `CommandPalette`
**Props:** `open: boolean, onClose: () => void, onNavigate: (page: PageRef) => void`
**Renders:** Full-screen dimmed overlay + centered modal. Input + grouped results list. Keyboard navigation required.

### `ResultCard`
**Props:** `entity: EntitySummary, score?: number, onClick: () => void`
**Renders:** A bordered row card: EntityBadge + title (bold) + secondary line (area, dates, byArtist depending on type) + optional relevance score badge (right-aligned, muted). On hover: bg-hover. Clickable full row.

### `FieldTable`
**Props:** `fields: Record<string, FieldValue>, schema: FieldSchema[], onLinkClick: (val: string, field: string) => void`
**Renders:** Two-column table (Field name left / Value right). Value cells: strings render as plain text; values matching known link patterns (byArtist, area, list items) render as clickable `--text-link` colored spans. Number fields right-aligned with muted type badge. Boolean fields as a small Yes/No chip.
**Extras:** "Show all fields" toggle to reveal empty/null rows. "Show raw JSON" link at bottom triggers JsonDrawer.

### `RelatedRail`
**Props:** `heading: string, mechanism: 'field-match'|'list-field'|'semantic', items: EntitySummary[], totalCount?: number, onViewAll: () => void`
**Renders:** Collapsible section with heading + mechanism label (small muted text e.g. "via byArtist"). Items as compact EntityBadge + title rows, max 5 shown. "View all N →" link if totalCount > 5. Loads asynchronously; shows inline skeleton while pending.

### `SemanticNeighborGrid`
**Props:** `neighbors: EntitySummary[], loading: boolean`
**Renders:** A 3-column (or 2 on narrow) grid of compact neighbor cards. Each card: EntityBadge + title + one secondary line. Label at section top: "Semantic neighbors · cosine · top 6". Separate async load from main page.

### `JsonDrawer`
**Props:** `payload: object, open: boolean, onClose: () => void`
**Renders:** A bottom-anchored drawer (300px default height, drag handle to resize up to 60vh). Contains the raw Qdrant point payload, syntax-highlighted JSON (string=green, number=blue, key=muted, null=red). Copy-to-clipboard button top-right.

### `Breadcrumb`
**Props:** `stack: PageRef[]`
**Renders:** Horizontal list of crumb links separated by `›`. Last item non-clickable (current page). Truncates to last 4 crumbs on narrow viewports with a `…` collapse.

### `TypeBrowseGrid`
**Props:** `typeCounts: Record<EntityType, number>, onTypeSelect: (t: EntityType) => void`
**Renders:** 4-column (2 on mobile) grid of type tiles. Each tile: large icon, type name, entity count. Used only on the landing page.

### `SkeletonBlock`
**Props:** `width?: string, height?: string, variant?: 'text'|'rect'`
**Renders:** A gray shimmer rectangle. Used to compose page skeleton loaders. Shimmer animation defined once in global CSS and applied via class.

### `StatusBadge`
**Props:** `variant: 'success'|'warning'|'error'|'info', label: string`
**Renders:** A small pill used for connection status, type indicators, relevance scores.

---

## 7. Page Routing (for engineer reference)

```
/                          → Landing / global search
/browse/:entityType        → Results list filtered by type
/search?q=&type=           → Search results page
/entity/:id                → Entity page (id = Qdrant point id or MB UUID)
```

All navigation is client-side (React Router or equivalent). No SSR required; this is a local-first dev tool.

---

## 10-Line Summary for the PM

The schema browser is a dark-mode, information-dense graph explorer styled like a refined developer tool — think Sourcegraph meets a Wikipedia article. A persistent left rail lets users filter by entity type (Artist, Recording, Place, etc.), each visually distinct via a small accent color and icon. A Cmd-K command palette is the primary navigation surface, enabling instant jump-to-entity or search without reaching for a mouse. Entity pages are structured like wiki articles: a typed header, a field table with clickable field values that trigger new searches, a related-entities section (honest about whether links come from field matches or semantic vector neighbors), and a raw-JSON drawer for power users. Results pages use compact scored cards with secondary info lines. The design relies entirely on React + Vite + CSS Modules — no heavy UI library — keeping the component count small: roughly 14 named components that cover all screens. Navigation history is maintained as a breadcrumb/back-stack in sessionStorage, giving wiki-style traversal without locking users into browser history. Loading states use shimmer skeletons (not spinners); errors are inline and non-blocking. The full palette uses 9 distinct entity-type accent colors against a near-black (#0d0f12) base, with light mode available via a data attribute. Keyboard shortcuts (J/K for lists, Enter to open, Esc to close) make the tool fast for repeated research sessions.
