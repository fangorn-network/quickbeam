// The seam between this corpus and the ported kernel.
//
// The kernel is sonder's, unchanged. Everything that makes it fit THIS data lives
// here, so the port stays diffable against its origin.
import type { Rec } from '../lib/types';
import type { TrackFeatures } from './types';

/**
 * Cosine similarity → squared L2 distance.
 *
 * **The one line of this port that can be wrong without anything throwing.**
 *
 * `reweight` computes `−distance / (2σ²)`, a Gaussian in SQUARED distance, because
 * sonder fed it ChromaDB's default `l2` metric. We produce cosine. For unit-norm
 * vectors the two are exactly related:
 *
 *   ‖a − b‖² = ‖a‖² + ‖b‖² − 2·a·b = 2 − 2·cos
 *
 * Both sides here are unit-norm — the document side by `matryoshka()`, the query
 * side by the same function — so this is an identity, not an approximation. It is
 * asserted against directly-computed L2 in scripts/check-kernel.ts, because getting
 * it wrong would just make every weight quietly mis-scaled.
 */
export const cosToDistance = (cos: number): number => 2 - 2 * cos;

const splitList = (s: unknown): string[] =>
  typeof s === 'string'
    ? s.split(',').map((x) => x.trim().toLowerCase()).filter(Boolean)
    : [];

/**
 * One record + its vector → the features the kernel accumulates taste over.
 *
 * Three of sonder's four taxonomy dimensions map onto real Audius data:
 *   genres  ← `genre`  (compound genres like "Tech House, Acid Groove" split)
 *   moods   ← `mood`
 *   themes  ← `tags`   (artist-applied, the closest thing we have to themes)
 *
 * `contexts` is deliberately left EMPTY. We have three real dimensions, not four,
 * and `dimScore` already returns its floor for an empty dimension, so an unused
 * dimension is neutral rather than harmful. The tempting filler — publisher — would
 * teach the kernel to prefer one wallet over the other, which is the one bias this
 * demo must not have.
 */
export function toFeatures(rec: Rec, embedding: Float32Array): TrackFeatures {
  const f = rec.fields;
  return {
    embedding,
    // Artist affinity keys on this; falling back to the record's own id keeps a
    // track with no artist from colliding with every other artist-less track under
    // a shared '' key.
    artistId: (f.artistId as string) || rec.id,
    genres: splitList(f.genre),
    moods: splitList(f.mood),
    themes: splitList(f.tags),
    contexts: [],
    // Our duration is seconds; the kernel's preference Gaussian is in ms.
    durationMs: Number(f.duration ?? 0) * 1000,
  };
}
