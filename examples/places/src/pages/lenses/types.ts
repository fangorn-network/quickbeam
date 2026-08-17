// Shared props every lens receives from the Discover shell. The shell does the
// single retrieval; lenses are presentational over the same `items` pool.
import type { EntitySummary } from '../../lib/types';

export interface LensProps {
  items: EntitySummary[];
  loading: boolean;
  error: 'network' | 'other' | null;
  // The raw user query (for empty-state copy and the Answer lens's planner).
  query: string;
  onOpen: (e: EntitySummary) => void;
}
