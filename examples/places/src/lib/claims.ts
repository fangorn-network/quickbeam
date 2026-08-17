// Business-profile ownership — the "claim-ready" seam.
//
// Every shard row carries an `owner` (the publisher who listed it). A *claim* is a
// separate, stronger assertion: the real business owner proving control of the
// profile. That lives on-chain (Phase 4: a Base registry mapping placeId → claimant
// address, written by a Privy embedded wallet — see docs/SOCIAL_ROADMAP.md).
//
// Until that ships, `useClaim` is a stub that reports "unclaimed" for everything.
// Keeping it behind this hook means Phase 4 is a drop-in: replace the body with a
// public on-chain read and the profile UI lights up with verified badges + tips,
// with zero changes at the call sites. The read is public knowledge, not a private
// server — so it never breaks the "intent is private" thesis.

export interface ClaimState {
  /** Whether a verified owner has claimed this profile. */
  claimed: boolean;
  /** The claimant's address when claimed (Phase 4); null until then. */
  claimant: string | null;
  /** True while an async on-chain read is in flight (always false in the stub). */
  loading: boolean;
}

// Phase 4 replaces this with a registry read keyed by placeId. Today: nothing is
// claimed, so every profile shows the "claim this business" call-to-action.
export function useClaim(placeId: string | null): ClaimState {
  void placeId;
  return { claimed: false, claimant: null, loading: false };
}

// Abbreviate a 0x address for display: 0x147c…5ef6.
export function shortAddr(a?: string | null): string {
  if (!a) return '';
  return a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a;
}
