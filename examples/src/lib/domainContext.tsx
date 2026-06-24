// Loads the active Domain once and provides it to the tree. Everything schema-aware
// reads from `useDomain()`.
import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Domain, loadDomain } from './domain';

const DomainContext = createContext<Domain | null>(null);

export function DomainProvider({ children }: { children: ReactNode }) {
  const [domain, setDomain] = useState<Domain | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadDomain().then((d) => {
      if (!cancelled) setDomain(d);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!domain) {
    return (
      <div style={{ padding: '2rem', color: 'var(--text-secondary)', fontFamily: 'system-ui' }}>
        Loading domain…
      </div>
    );
  }

  return <DomainContext.Provider value={domain}>{children}</DomainContext.Provider>;
}

export function useDomain(): Domain {
  const d = useContext(DomainContext);
  if (!d) throw new Error('useDomain must be used within <DomainProvider>');
  return d;
}
