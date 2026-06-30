"""Derived rating signals computed purely from a place's own (rating, review-count)
pair — no corpus pass, no user input, no network. Generalizes to anywhere on Earth
and ships inside the static snapshot.

Two facts a general LLM can't give you, because they're relative to a place's own
attention rather than the open web's average:

  - hidden-gem      : genuinely loved but under-discovered (high rating, few reviews)
  - crowd-favorite  : loved and proven at volume (high rating, many reviews)

The hidden-gem signal is, in effect, "the local secret" — surfaced from two numbers
instead of from community notes. `gemScore` is a continuous 0..1 sort key
(quality x obscurity); `ratingTier` is the badge. Both are omitted when there is no
rating to stand on.
"""
import math

# A modest prior the raw rating is shrunk toward, so a 5.0 from 3 reviews doesn't
# masquerade as quality. _K is the pseudo-count ("reviews of prior belief").
_PRIOR, _K = 3.8, 12

# Review count at which a place reads as fully mainstream *anywhere* — rural lake
# town or dense city alike. log-scaled so the obscurity signal is density-agnostic,
# which is what lets the same thresholds generalize beyond any one region.
_MAINSTREAM = 1500
_CAP = math.log10(_MAINSTREAM)


def rating_signal(rating, n) -> dict:
    """Return {"gemScore", optional "ratingTier"} for a (rating, review-count) pair,
    or {} when either is missing. Pure function — safe to call per entity at
    schema-gen time, no corpus required."""
    if not rating or not n:
        return {}

    # Bayesian-shrunk quality in [0,1]: a gem must be genuinely good after shrink,
    # so tiny-n outliers (5.0 from a handful of reviews) don't qualify on rating alone.
    adjusted = (n * rating + _K * _PRIOR) / (n + _K)
    quality = max(0.0, min(1.0, adjusted - 4.0))

    # Obscurity decays with attention; ~_MAINSTREAM reviews -> 0.
    obscurity = max(0.0, 1.0 - math.log10(max(n, 1)) / _CAP)
    score = round(quality * obscurity, 3)

    if adjusted >= 4.5 and obscurity >= 0.30 and score >= 0.20:
        tier = "hidden-gem"
    elif adjusted >= 4.3 and obscurity <= 0.18:
        tier = "crowd-favorite"
    else:
        tier = None

    out = {"gemScore": score}
    if tier:
        out["ratingTier"] = tier
    return out
