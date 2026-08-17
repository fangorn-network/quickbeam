// Query understanding — turn a natural-language search into (semantic intent)
// + (structured constraints). The vector search is great at *vibe* ("cozy
// lakeside supper club") but blind to *facts* ("open now", "under $$",
// "kid-friendly"): those live in the payload (priceLevel / hours / amenities /
// rating) and must be matched exactly, not fuzzily. This splits a raw query into
// the part the embedding should see and a StructuredFilters object the existing
// shard/Qdrant filter pipeline already enforces (see qdrant.ts + shards.ts).
//
// Deliberately rule-based: deterministic, zero-latency, debuggable, and no
// dependency on the flaky in-browser model. extract() is the single seam where an
// LLM pass could later augment or replace these rules without touching callers.
import type { StructuredFilters } from './qdrant';

export interface Interpretation {
  // Text to embed for the vector search (constraint-only phrases removed).
  semantic: string;
  // Structured constraints lifted out of the query, ready to merge into the
  // filters the caller already passes to search()/scroll().
  filters: StructuredFilters;
  // Human-readable summary of what was understood, for a transparency chip row.
  notes: string[];
  // "Hidden gems" — the derived rating-signal filter (lib/gem). Not a payload
  // field, so it's a flag the caller applies client-side over the pool, not part
  // of StructuredFilters.
  gemsOnly?: boolean;
}

// The amenity vocabulary actually present in the corpus (Google Places + OSM),
// mapped from the words people type. Values must match payload tokens exactly.
const AMENITY_PHRASES: { re: RegExp; amenity: string; note: string }[] = [
  { re: /\b(kid|child|children|family)[ -]?(friendly)?\b|\btoddlers?\b|\bwith kids\b/i, amenity: 'good for children', note: 'Kid-friendly' },
  { re: /\b(outdoor seating|patio|al fresco|outdoor dining|deck seating|eat outside)\b/i, amenity: 'outdoor seating', note: 'Outdoor seating' },
  { re: /\b(takeout|take[ -]?out|to[ -]?go|carry[ -]?out|takeaway)\b/i, amenity: 'takeout', note: 'Takeout' },
  { re: /\b(delivery|delivers?|deliver)\b/i, amenity: 'delivery', note: 'Delivery' },
  { re: /\b(dine[ -]?in|sit[ -]?down)\b/i, amenity: 'dine-in', note: 'Dine-in' },
  { re: /\b(cocktails?|craft cocktails|mixed drinks|old fashioned)\b/i, amenity: 'serves cocktails', note: 'Cocktails' },
  { re: /\b(on tap|craft beer|brews|beer garden|biergarten)\b/i, amenity: 'serves beer', note: 'Beer' },
  { re: /\b(wine list|wine bar|glass of wine)\b/i, amenity: 'serves wine', note: 'Wine' },
  { re: /\b(reservations?|reservable|book a table)\b/i, amenity: 'reservable', note: 'Takes reservations' },
  { re: /\b(live music|live band)\b/i, amenity: 'live music', note: 'Live music' },
];

// "Meta" constraints — pure modifiers that describe *requirements*, not the venue
// itself, so they're stripped from the embedded text (they only add noise to the
// vector) while their structured effect is kept.
const PRICE_CHEAP = /\b(cheap|inexpensive|budget|affordable|low[ -]?cost|good value)\b/i;
const PRICE_HIGH = /\b(upscale|fancy|fine dining|high[ -]?end|expensive|splurge|pricey)\b/i;
const OPEN_NOW = /\b(open (now|right now|late|today)|right now|open at this hour)\b/i;
const TOP_RATED = /\b(best|top[ -]?rated|highly[ -]?rated|highest[ -]?rated|well[ -]?reviewed|great reviews|top notch)\b/i;
// "Hidden gems" — genuinely-loved but under-discovered. A facet a general search
// can't reproduce; matched to the derived rating signal, not the open web.
const HIDDEN_GEMS = /\b(hidden gems?|underrated|under[- ]the[- ]radar|off the beaten (path|track)|secret spots?|local secret|undiscovered|lesser[- ]known|hole[- ]in[- ]the[- ]wall)\b/i;

// Rating floor for "best/top-rated". Kept modest (not 4.8) because the filter
// EXCLUDES entries lacking a rating (e.g. OSM-only venues), so an aggressive
// floor would silently drop half the corpus. 4.3 keeps well-liked OSM-less gaps
// from dominating without nuking everything unrated… see note in matchesFilters.
const TOP_RATED_FLOOR = 4.3;

export function interpretQuery(raw: string): Interpretation {
  const filters: StructuredFilters = {};
  const notes: string[] = [];
  let text = ` ${raw} `;

  // Amenities: add the filter but KEEP the words in the semantic text — they help
  // the vector too ("patio" is both a constraint and a descriptive cue).
  const amenities: string[] = [];
  for (const { re, amenity, note } of AMENITY_PHRASES) {
    if (re.test(text) && !amenities.includes(amenity)) {
      amenities.push(amenity);
      notes.push(note);
    }
  }
  if (amenities.length) filters.amenities = amenities;

  // Price: strip (a pure requirement, not descriptive of the place's content).
  if (PRICE_CHEAP.test(text)) {
    filters.priceLevels = ['$', '$$'];
    notes.push('Budget-friendly');
    text = text.replace(PRICE_CHEAP, ' ');
  } else if (PRICE_HIGH.test(text)) {
    filters.priceLevels = ['$$$', '$$$$'];
    notes.push('Upscale');
    text = text.replace(PRICE_HIGH, ' ');
  }

  // Open now: strip.
  if (OPEN_NOW.test(text)) {
    filters.openNow = true;
    notes.push('Open now');
    text = text.replace(OPEN_NOW, ' ');
  }

  // Top-rated: strip the modifier, keep any noun it qualified ("best tacos" →
  // semantic "tacos" + ratingGte).
  if (TOP_RATED.test(text)) {
    filters.ratingGte = TOP_RATED_FLOOR;
    notes.push('Top-rated');
    text = text.replace(TOP_RATED, ' ');
  }

  // Hidden gems: strip the phrase (it's a facet, not descriptive of the venue) and
  // raise the flag the caller applies over the pool.
  let gemsOnly = false;
  if (HIDDEN_GEMS.test(text)) {
    gemsOnly = true;
    notes.push('Hidden gems');
    text = text.replace(HIDDEN_GEMS, ' ');
  }

  const semantic = text.replace(/\s+/g, ' ').trim();
  return {
    // If the query was *only* constraints ("open now cheap"), keep the original so
    // the vector still has something to rank by. But a bare "hidden gems" should
    // browse the gem set (empty semantic → scroll + client gem filter), not search
    // the literal words.
    semantic: semantic || (gemsOnly ? '' : raw.trim()),
    filters,
    notes,
    gemsOnly,
  };
}

// ---- LLM augmentation (the seam promised above) ----
// The embedded model can catch fuzzy phrasings the regexes miss ("date-night
// spot" → upscale + cocktails) but only into the SAME closed vocabulary, so its
// output stays a classification we can trust, never free invention. The model is
// handed exactly this menu; intentFromLlmJson maps its JSON back through the same
// constants the rules use, and mergeInterpretations folds it onto the rule result
// (rules win on conflict — they're precise and free).

// The amenity tokens the model is allowed to choose from (must match payload
// tokens exactly; the prompt enumerates these so it can't invent one).
export const LLM_AMENITY_TOKENS = AMENITY_PHRASES.map((a) => a.amenity);
const ALLOWED_AMENITIES = new Set(LLM_AMENITY_TOKENS);
const AMENITY_NOTE = new Map(AMENITY_PHRASES.map((a) => [a.amenity, a.note]));

// The structured read the LLM contributes — a delta over the rules. It never
// touches the embedded `semantic` text (the rules own that); it only adds the
// payload constraints/flags it’s confident the query implies.
export interface LlmIntent {
  filters: StructuredFilters;
  notes: string[];
  gemsOnly?: boolean;
}

// Map the model's closed-vocabulary JSON menu into an LlmIntent using the very
// same constants/floors the rule path uses, so a regex hit and a model hit on the
// same constraint are indistinguishable downstream. Unknown keys/values are dropped.
export function intentFromLlmJson(obj: Record<string, unknown>): LlmIntent {
  const filters: StructuredFilters = {};
  const notes: string[] = [];
  let gemsOnly = false;

  if (obj.price === 'cheap') {
    filters.priceLevels = ['$', '$$'];
    notes.push('Budget-friendly');
  } else if (obj.price === 'upscale') {
    filters.priceLevels = ['$$$', '$$$$'];
    notes.push('Upscale');
  }
  if (obj.openNow === true) {
    filters.openNow = true;
    notes.push('Open now');
  }
  if (obj.topRated === true) {
    filters.ratingGte = TOP_RATED_FLOOR;
    notes.push('Top-rated');
  }
  if (obj.gems === true) {
    gemsOnly = true;
    notes.push('Hidden gems');
  }
  const amenities: string[] = [];
  if (Array.isArray(obj.amenities)) {
    for (const a of obj.amenities) {
      if (typeof a === 'string' && ALLOWED_AMENITIES.has(a) && !amenities.includes(a)) {
        amenities.push(a);
        const n = AMENITY_NOTE.get(a);
        if (n) notes.push(n);
      }
    }
  }
  if (amenities.length) filters.amenities = amenities;

  return { filters, notes, gemsOnly: gemsOnly || undefined };
}

// Fold the model's delta onto the rule-based interpretation. The rules' semantic
// text and precise hits are authoritative; the model only ADDS constraints they
// missed. Notes are unioned (deduped) so the "Understood" chip row reflects both.
export function mergeInterpretations(rules: Interpretation, llm: LlmIntent): Interpretation {
  return {
    semantic: rules.semantic,
    filters: mergeFilters(rules.filters, llm.filters), // base (rules) wins on conflict
    notes: Array.from(new Set([...rules.notes, ...llm.notes])),
    gemsOnly: rules.gemsOnly || llm.gemsOnly,
  };
}

// Merge interpreted constraints into filters the caller already holds (UI toggles
// like upcomingOnly/dateWindow). Caller-set values win on conflict.
export function mergeFilters(base: StructuredFilters, add: StructuredFilters): StructuredFilters {
  return {
    ...add,
    ...base,
    amenities: [...(add.amenities ?? []), ...(base.amenities ?? [])].length
      ? Array.from(new Set([...(add.amenities ?? []), ...(base.amenities ?? [])]))
      : undefined,
  };
}
