// Loads per-type counts once (with light caching) and tracks Qdrant reachability.
import { useEffect, useState } from 'react';
import { ENTITY_TYPES } from '../lib/types';
import type { EntityType } from '../lib/types';
import { countByType, QdrantError } from '../lib/qdrant';

type Counts = Partial<Record<EntityType, number | null>>;

let cache: Counts | null = null;

export function useTypeCounts() {
  const [counts, setCounts] = useState<Counts>(cache ?? {});
  const [connectionError, setConnectionError] = useState(false);

  useEffect(() => {
    if (cache) return;
    let cancelled = false;
    (async () => {
      const next: Counts = {};
      let sawNetworkError = false;
      await Promise.all(
        ENTITY_TYPES.map(async (t) => {
          try {
            next[t] = await countByType(t);
          } catch (e) {
            next[t] = null;
            if (e instanceof QdrantError && e.kind === 'network') sawNetworkError = true;
          }
        }),
      );
      if (cancelled) return;
      cache = next;
      setCounts(next);
      setConnectionError(sawNetworkError);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return { counts, connectionError };
}
