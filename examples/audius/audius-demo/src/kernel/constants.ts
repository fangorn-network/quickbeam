/**
 * constants.ts
 *
 * Embedding dimension and default kernel parameters.
 *
 * ── Ported from sonder (app/src/renderer/src/kernel/constants.ts) ─────────────
 * Two deltas from the original, and only two:
 *
 *   D          was 384 (all-MiniLM-L6-v2); here 256 (nomic-embed-text-v1.5
 *              truncated by the matryoshka transform — see lib/matryoshka.ts).
 *
 *   DEFAULTS   unchanged. They were calibrated against a 384-d MiniLM point
 *              cloud, so they are a starting point here, not a fitted answer.
 *              Every one is overridable per call; if the rail feels too tight or
 *              too jumpy, lambda_max / sigma_base / gamma_reg are the knobs.
 *              Re-baking them on a guess would be worse than leaving them
 *              honest and exposed.
 *
 * tau_art: measured, and deliberately left alone
 * ──────────────────────────────────────────────
 * The recommendation rail once collapsed onto ONE artist — after three plays,
 * 12 of 12 slots. Three of reweight's four terms correlate with "same artist":
 * a catalogue clusters tightly so the distance term favours it, tau_cat adds
 * that artist's genres and moods, and tau_art piles an explicit ≈2.97× bonus
 * onto tracks already nearest by geometry. tau_art looks like the culprit.
 *
 * Lowering it was tried and REJECTED on measurement. With the per-artist display
 * cap in lib/graph.ts (MAX_PER_ARTIST) in place, the rail holds 8 distinct
 * artists out of 12 at tau_art 2.00, 1.50 and 1.00 alike — the cap saturates the
 * diversity, so the model change buys nothing visible. It does cost: a followed
 * artist with a thin catalogue falls from rail position 10 to rank 451, i.e. out
 * of sight, so "follow three artists" stops showing you all three.
 *
 *     tau_art (with cap)   2.00 → 8/12, thin artist rank 10
 *                          1.50 → 8/12, rank 57
 *                          1.00 → 8/12, rank 451
 *
 * Fix concentration at the display layer, not here. Recorded so the next person
 * to reach for this knob knows it was already measured.
 *
 * Calibration notes
 * ─────────────────
 * lambda_max        Must exceed the radius of the largest cluster you want the
 *                   kernel to escape. Measure empirically against your ChromaDB
 *                   point cloud.
 *
 * sigma_max         Caps spread at 2× sigma_base by default.
 *
 * gamma_base        Base repulsion step. Actual magnitude = gamma_base × (1+η).
 *                   Decoupled from σ so repulsion does not weaken as spread
 *                   contracts on play.
 *
 * theta_B           Suppression threshold. With delta_skip=1, an artist is
 *                   permanently blacklisted after 3 genuine-dislike skips.
 *
 * art_logistic_c    Logistic steepness. c=3 gives f_art ≈ 0.05 at artists=−1
 *                   (strong suppression) and f_art ≈ 1.95 at artists=+1.
 *
 * fatigue_threshold Artist EMA value above which a skip is interpreted as
 *                   session fatigue rather than genuine dislike. With
 *                   beta_artist=0.7, artists[a] ≈ 0.30 after 1 play and
 *                   ≈ 0.51 after 2 plays. The default of 0.3 means a single
 *                   prior play is enough to activate fatigue protection.
 *                   Raise to require more listening history before protecting
 *                   an artist from the neg accumulator.
 */

import type { KernelParams } from './types.ts'

/** Embedding dimension of THIS corpus. Must match the baked shard's `dim`. */
export const D = 256

export const DEFAULTS: Required<KernelParams> = {
  // geometric
  alpha:             0.10,
  beta:              0.30,
  lambda_max:        0.40,
  sigma_base:        0.50,
  sigma_max:         1.00,
  rho:               0.80,
  epsilon:           1e-6,
  prior_k:           1.00,
  skip_window:       20,

  // repulsion
  gamma_base:        0.15,
  gamma_reg:         2.00,

  // temperature
  temp_base:         1.00,
  temp_max:          3.00,

  // taste
  alpha_taste:       0.30,
  taste_decay:       0.05,
  skip_taste_pen:    0.20,

  // artist
  beta_artist:       0.70,
  art_logistic_c:    3.00,

  // duration
  rho_duration:      0.80,
  dur_sigma_ms:      60_000,

  // log-weight coefficients
  tau_cat:           1.00,
  tau_art:           2.00,
  tau_dur:           0.50,
  cat_floor:         0.01,

  // persistent suppression
  delta_skip:        1.00,
  theta_B:           3.00,

  // session fatigue
  fatigue_threshold: 0.30,
}