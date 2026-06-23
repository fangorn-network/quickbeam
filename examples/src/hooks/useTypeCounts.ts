// Loads per-type counts once (with light caching) and tracks Qdrant reachability.
// The set of types comes from the active Domain, not a hardcoded list.
import { useEffect, useState } from 'react';
import { countByType, QdrantError } from '../lib/qdrant';
import { useDomain } from '../lib/domainContext';

type Counts = Record<string, number | null>;

let cache: Counts | null = null;

export function useTypeCounts() {
  const domain = useDomain();
  const [counts, setCounts] = useState<Counts>(cache ?? {});
  const [connectionError, setConnectionError] = useState(false);

  useEffect(() => {
    if (cache) return;
    let cancelled = false;
    (async () => {
      const next: Counts = {};
      let sawNetworkError = false;
      await Promise.all(
        domain.entityTypes.map(async (t) => {
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
  }, [domain]);

  return { counts, connectionError };
}
