// Client mirror of quickbeam/pipelines/gem.py — derives the "hidden gem / local
// favorite" signal from a place's own (rating, review-count) pair. Pure and
// corpus-free, identical to the baked formula, so the badge & sort light up on the
// CURRENT static snapshot with no re-bake (rating + userRatingCount are already in
// the shard). Once the pipeline bakes `ratingTier`/`gemScore` in, those win — see
// ratingSignalOf. Keep the constants in sync with gem.py if either side changes.

export type RatingTier = 'hidden-gem' | 'crowd-favorite';

const PRIOR = 3.8; // modest prior the raw rating is shrunk toward
const K = 12; // pseudo-count ("reviews of prior belief")
const MAINSTREAM = 1500; // review count at which a place reads as fully mainstream anywhere
const CAP = Math.log10(MAINSTREAM);

export interface RatingSignal {
  gemScore: number; // 0..1 continuous sort key (quality x obscurity)
  tier: RatingTier | null; // the badge, when it clears a bar
}

export function ratingSignal(rating?: number | null, n?: number | null): RatingSignal | null {
  if (!rating || !n) return null;
  // Bayesian-shrunk quality in [0,1] — tiny-n 5.0s don't qualify on rating alone.
  const adjusted = (n * rating + K * PRIOR) / (n + K);
  const quality = Math.max(0, Math.min(1, adjusted - 4));
  // Obscurity decays with attention; ~MAINSTREAM reviews -> 0 (density-agnostic).
  const obscurity = Math.max(0, 1 - Math.log10(Math.max(n, 1)) / CAP);
  const gemScore = Math.round(quality * obscurity * 1000) / 1000;

  let tier: RatingTier | null = null;
  if (adjusted >= 4.5 && obscurity >= 0.3 && gemScore >= 0.2) tier = 'hidden-gem';
  else if (adjusted >= 4.3 && obscurity <= 0.18) tier = 'crowd-favorite';
  return { gemScore, tier };
}

// Prefer the baked fields when a re-bake has added them; otherwise derive client-side.
export function ratingSignalOf(fields: Record<string, unknown>): RatingSignal | null {
  const bakedTier = fields.ratingTier;
  const bakedScore = fields.gemScore;
  if (typeof bakedTier === 'string' || typeof bakedScore === 'number') {
    return {
      gemScore: typeof bakedScore === 'number' ? bakedScore : 0,
      tier: bakedTier === 'hidden-gem' || bakedTier === 'crowd-favorite' ? bakedTier : null,
    };
  }
  const rating = typeof fields.rating === 'number' ? fields.rating : null;
  const n = typeof fields.userRatingCount === 'number' ? fields.userRatingCount : null;
  return ratingSignal(rating, n);
}

export const TIER_LABEL: Record<RatingTier, string> = {
  'hidden-gem': 'Hidden gem',
  'crowd-favorite': 'Local favorite',
};
