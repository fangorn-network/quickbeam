# Social roadmap: discovery → planning → sharing → identity

A staged plan for turning the local-first browser ([`examples/`](../examples)) from a
solo discovery tool into a **social tourism app** — without giving up the thesis that
makes it different.

> **Read [SEMANTIC_CDN.md](SEMANTIC_CDN.md) first.** This document assumes the
> `shards` data source: the app downloads a baked Semantic CDN snapshot and does all
> search / ranking / embedding **in the browser**. Nothing here reintroduces a query
> server.

---

## The one rule everything obeys

**"Knowledge is public, intent is private."** The social layer is allowed to learn
**identity** and **explicit publish actions**. It is never allowed to learn **queries,
browsing, or what you looked at.**

In practice that means there is **no new backend**. The only servers that exist are:

| Server | Sees | When |
|---|---|---|
| Static host (app bundle + CDN shards) | which domain was pulled | on load |
| Privy (Phase 3) | email + IP | only at explicit login |
| Base / chain RPC (Phase 4, out of scope here) | public on-chain reads/writes | claim / tip |

Search, browse, ranking, and itineraries stay entirely client-side. Where a feature
*could* leak intent (map tiles, link shorteners, analytics), we either avoid it or
disclose it — see each phase.

---

## What the data already gives us

The `bars` domain (Northwoods Wisconsin: bars, supper clubs, resorts + local events) is
already shaped for tourism. Per the baked manifest and shard rows:

- **Business**: `coordinates` ("lat,lng"), `rating`, `userRatingCount`, `priceLevel`,
  `categories[]`, `amenities` (JSON-encoded string), `hours` (string),
  `locality`, `googleMapsUri`, `editorialSummary`, `owner` (0x address).
- **Event**: `startDate` / `endDate`, `isFree`, `isPast`, `locality`, often
  `coordinates`, sometimes `hostBusinessId` / `hostBusinessName`, `organizerName`.

And the code seams already exist:

- [`lib/qdrant.ts`](../examples/src/lib/qdrant.ts) `StructuredFilters` —
  `ratingGte`, `priceLevels`, `categories`, `localities`, `amenities`, `upcomingOnly`.
- [`lib/shards.ts`](../examples/src/lib/shards.ts) — `shardSearch`, `shardNear`,
  `shardEventsForHost`, `shardBusinessByPlaceId`, in-memory `matchesFilters`.
- [`lib/geo.ts`](../examples/src/lib/geo.ts) — `parseCoords`, `haversineKm`.
- [`lib/embed.ts`](../examples/src/lib/embed.ts) — in-browser query embedding.

So most of Phase 1 is **surfacing capabilities that already compute**, not building new
infrastructure.

---

## Phase 1 — Tourism enhancements (low-hanging fruit)

Ships first, no login, no map. These make the app *useful for planning a visit*.

> **Status (in progress).** Items 4, plus the parse/badge half of 2 and the facet
> half of 3, pre-existed. Now implemented: **My Trip + shareable link (5)**,
> **copy-link share (6)**, **free/paid + date-window event filters (1)**, and the
> **open-now list filter + result-card badges (2/3)**. All client-side, no backend.

1. **Time-aware event discovery — "this weekend / near X".**
   `upcomingOnly` already drops past events (`fields.isPast`); `isFree` and `locality`
   are filterable. Add UI for **date-window** ("today", "this weekend", a range) over
   `startDate`, plus a free/paid toggle. This is the single highest-value tourism query
   and it's nearly free given the filters.

2. **"Open now" for businesses.**
   `hours` is present but as a display string
   (`"Monday: 11:00 AM – 2:00 AM; …"`). Write a small parser → structured weekly hours →
   an `openNow` predicate evaluated client-side against the user's local clock. Surface
   as a filter + an "Open now" badge on cards.

3. **Category / amenity facets.**
   `categories[]` and `amenities` already filter (`matchesFilters` parses the
   JSON-encoded amenities). Promote them to visible facet chips ("outdoor seating",
   "live music", "supper club") so combinations like *supper clubs with outdoor seating*
   are one tap, not a typed query.

4. **Event ↔ venue panel.**
   `shardEventsForHost(placeId)` is built. On a Business profile, show "What's on here";
   on an Event with a `hostBusinessId`, link back to the venue. Honest hard edges where
   the data actually has them (unlike the soft name-search links elsewhere).

5. **Shareable itineraries — "My Trip" (no backend).**
   A **Trip** is a client-side, ordered list of pinned `placeId`s + `eventId`s
   (+ optional notes), persisted in `localStorage`.
   - **Add to trip** from any card / profile.
   - **Share = encode the trip into the URL _hash fragment_** (`#trip=<compressed b64>`),
     **not** the path or query string. The fragment is never sent in an HTTP request, so
     even the static host's access logs never see who shared which trip. The recipient's
     browser decodes it and rehydrates each item from the already-loaded shard data
     (items are just IDs against public knowledge).
   - No share service, no database, **no link shortener** (a shortener would *read* the
     trip — avoid). The link is self-contained.
   - Order stops by proximity later using `haversineKm` (a precursor to the deferred map).

6. **Shareable business / event profiles — copyable link.**
   Entity pages (`/entity/:pointId`) are already deep-linkable. Add an explicit **Copy
   link** affordance and clean share metadata. No new state — the URL already resolves
   against the loaded snapshot. This is the cheapest "social" win and seeds organic
   growth.

> **Privacy note for Phase 1:** everything here is local. Shared links carry only public
> IDs/state and (for trips) live in the unsent hash fragment. Nothing leaks intent.

---

## Phase 2 — Richer profiles (still no login)

Promote Business entity pages into proper **profiles**: parsed hours, amenities, price,
rating, hosted events (Phase 1 #4), `editorialSummary`, external `googleMapsUri` link,
"Add to trip", and "Copy link". All from existing shard data — no backend. This sets the
visual/IA foundation that Phase 3 and 4 attach identity to.

> **Status (done), scoped to the data.** Two things this phase imagined have **no
> data to render** in the `bars` corpus: `imageUrl` is null (no hero images), and
> `reviews` are bare attribution strings ("Jane Thiel on Cira's…"), not review bodies
> with ratings. Reviewer/review *lists* already surface generically via Connections.
> The basics (hours/amenities/rating/lede/hosted-events) shipped in Phase 1. So Phase 2
> delivered its durable purpose — the **claim-ready ownership layer**:
> - `lib/claims.ts` — a `useClaim(placeId)` **stub seam** that Phase 4 swaps for a
>   public on-chain registry read (placeId → claimant). Zero call-site changes later.
> - `components/ProfileOwnership.tsx` — a provenance/claim strip on Business profiles:
>   "Listed by 0x…" + an honest "Is this your business?" CTA (explainer, not a faked
>   tx) when unclaimed; a verified "✓ Claimed" badge slot when claimed.
> - `businessStatus` polish — a non-operational status ("Permanently/Temporarily
>   closed") now overrides the hours-derived "Open now" on both profiles and result
>   cards, so stale hours can't mislabel a closed place.

---

## Phase 3 — Privy login (identity, not a backend)

- Add `@privy-io/react-auth`; wrap the app in `PrivyProvider`. **Email login → Privy
  mints an embedded wallet** transparently, so a non-crypto user still gets an address —
  the same key the Phase 4 claim/tip flow uses. Login now is forward-compatible with the
  contract work; no rework later.
- **Login is never required to discover.** Anonymous users keep the full Phase 1–2
  experience. Login gates only *actions*: (eventually) claim, tip, and optionally
  "save trips across this account."
- Privy's servers see email + IP at login — that is identity by definition, and
  acceptable. The hard line holds because **no query or browse event is ever sent
  anywhere.** Search stays in the browser exactly as today.

> **Status (done).** `@privy-io/react-auth` wraps the app in `main.tsx` with
> embedded-wallet creation (`createOnLogin: 'users-without-wallets'`) on **Base**
> (`lib/network.ts`: Base Sepolia in dev, Base mainnet in prod; `VITE_NETWORK`
> override). App id in `lib/config.ts` (`VITE_PRIVY_APP_ID`).
> `components/AuthButton.tsx` adds a TopBar "Sign in" → account chip + logout menu.
> Login is fully optional: if Privy isn't `ready` (e.g. offline) the button just
> hides and discovery is unaffected. `ProfileOwnership`'s claim CTA now demonstrates
> the gated-action pattern — "Sign in to claim →" calls `login()` when signed out,
> "Claim this profile" (the explainer) when signed in. The actual claim write is
> Phase 4.
>
> **Two operational notes:** (1) the Privy dashboard must allowlist the app's
> origin (e.g. `http://localhost:5173`) or the login modal won't open. (2) Privy is a
> heavy dependency — the main JS chunk jumped to ~830 KB gzip. Lazy-loading Privy
> (and the transformers embedder) behind dynamic `import()` is worth doing before any
> public deploy.

---

## Phase 4 — Public, claim-ready business profiles (Base contract — mostly out of scope)

The mechanism is still open; the shape is not. Each shard row already carries an `owner`
0x address — the natural claim anchor.

- **Claimed state is a public on-chain _read_**, not a private server: an on-chain
  registry maps `placeId → claimant address`. The profile reads it and shows a verified
  badge + **tip** button when claimed, or "Is this your business? Claim it." when not.
- **Claim + tip are on-chain _writes_** signed by the Privy embedded wallet. Start with
  **tips only** — a single `tip(placeId)` payable call, no escrow, no marketplace —
  mirroring the bolt-on posture of the x402 note in [SEMANTIC_CDN.md](SEMANTIC_CDN.md).
- Claim verification mechanism is **TBD** (candidates: Privy email vs. a verified
  business domain/phone, or an off-chain attestation gating the on-chain write). Decide
  before building.

---

## Phase 5 — Map (deferred to the end, on purpose)

A secondary, toggle-able view (the list/search browser stays primary). Pins from
`coordinates` (already parsed), "near me" via `shardNear`, all Phase 1 filters driving
both views at once.

**Open privacy decision:** map *tiles* leak the viewport (roughly *where you're
looking*) to the tile provider — that bumps the hard line. Because the corpus is one
small region, **self-hosting tiles is feasible** and most on-thesis; alternatives are a
privacy-respecting provider or accepting + disclosing the leak. Settle this before
implem

---

## Explicit non-goals

- **No accounts/saved-lists backend.** The URL-fragment Trip replaces it.
- **No marketplace.** Payments are tips-only, later, on Base.
- **No query-level analytics.** Any product analytics must be event-level
  ("a trip was shared"), never query-level — query logging silently breaks the thesis.

---

## Sequencing summary

| Phase | Ships | New server? | Gated by login? |
|---|---|---|---|
| 1 — Tourism + sharing | time/open-now/facets, Trip, copy-link | no | no |
| 2 — Richer profiles | full Business profile pages | no | no |
| 3 — Privy | email login + embedded wallet | Privy (login only) | actions only |
| 4 — Claim + tip | on-chain claim/tip (mostly out of scope) | chain RPC (public) | yes |
| 5 — Map | secondary map view | tiles TBD | no |
