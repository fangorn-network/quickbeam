export type EntityType = 'Track' | 'Artist' | 'Playlist' | 'Genre' | 'Mood' | 'Tag';

export interface Fields {
  entityType?: string;
  id?: string;
  title?: string;
  artist?: string;
  artistId?: string;
  artistHandle?: string;
  genre?: string;
  mood?: string;
  tags?: string;
  description?: string;
  bio?: string;
  location?: string;
  handle?: string;
  kind?: string;
  isAlbum?: boolean;
  isReference?: boolean;
  isVerified?: boolean;
  vocabulary?: boolean;
  artworkCid?: string;
  avatarCid?: string;
  bannerCid?: string;
  duration?: number;
  playCount?: number;
  favoriteCount?: number;
  repostCount?: number;
  followerCount?: number;
  trackCount?: number;
  albumCount?: number;
  playlistCount?: number;
  releaseDate?: string;
  permalink?: string;
  text?: string;
  [k: string]: unknown;
}

/** One served record. `owner` is the wallet that published it — the whole demo. */
export interface Rec {
  id: string;
  entityType: EntityType | string;
  owner?: string;
  fields: Fields;
  score?: number;
}

export interface Edge {
  rel: string;
  from: string;
  to: string;
  fromType?: string;
  toType?: string;
}

export interface RelationGroup {
  rel: string;
  dir: 'out' | 'in';
  count: number;
  /** True when at least one neighbour was published by a DIFFERENT wallet. */
  crosses: boolean;
}

export interface Publisher {
  owner: string;
  /** Node counts by entity type, for the hero ledger. */
  counts: Record<string, number>;
  total: number;
  /** Display name, read from the publisher's own Artist record where it has one —
   *  so nothing in the UI hard-codes which artist this demo is about. */
  label?: string;
  /** The id of that Artist record, so the ledger can link straight to it. */
  labelId?: string;
}

export interface Stats {
  records: number;
  edges: number;
  publishers: Publisher[];
  /** Cross-publisher edges grouped by relation, biggest first. */
  linkset: Array<{ rel: string; count: number }>;
  linksetTotal: number;
  /** Vertices both publishers derived independently to the same content address. */
  converged: number;
  /**
   * The whole catalogue, as opposed to the slice that was downloaded.
   *
   * Every other field on `Stats` describes what is RESIDENT — what rails traverse,
   * what onboarding ranks, what the kernel scores over. This describes what is
   * SEARCHABLE. The two differ by ~46x here, so using the resident figure where the
   * UI makes its scale claim understates the catalogue badly.
   *
   * Optional: a bootstrap-only deployment (no hosted API) has no larger catalogue to
   * describe, and the UI falls back to the resident numbers, which are then correct.
   */
  catalogue?: {
    records: number;
    publishers: Array<{ owner: string; total: number; counts: Record<string, number> }>;
  };
}
