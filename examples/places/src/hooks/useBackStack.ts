// Wiki-style back stack / recent list, persisted in sessionStorage.
import { useCallback, useEffect, useState } from 'react';
import type { PageRef } from '../lib/types';

const KEY = 'sb.backstack.v1';
const MAX = 20;

function read(): PageRef[] {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as PageRef[]) : [];
  } catch {
    return [];
  }
}

function write(stack: PageRef[]) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(stack));
  } catch {
    /* ignore quota */
  }
  window.dispatchEvent(new Event('sb-backstack-change'));
}

export function pushPage(page: PageRef) {
  const stack = read();
  // Dedupe consecutive identical hrefs.
  if (stack.length && stack[stack.length - 1].href === page.href) return;
  const next = [...stack, page].slice(-MAX);
  write(next);
}

export function useBackStack() {
  const [stack, setStack] = useState<PageRef[]>(read);

  useEffect(() => {
    const sync = () => setStack(read());
    window.addEventListener('sb-backstack-change', sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener('sb-backstack-change', sync);
      window.removeEventListener('storage', sync);
    };
  }, []);

  const pop = useCallback((): PageRef | null => {
    const cur = read();
    if (cur.length < 2) return null;
    const next = cur.slice(0, -1);
    write(next);
    return next[next.length - 1];
  }, []);

  // Reverse-chronological, deduped by href, for the Recent list.
  const recent = dedupeRecent(stack);

  return { stack, recent, pop, canGoBack: stack.length > 1 };
}

function dedupeRecent(stack: PageRef[]): PageRef[] {
  const seen = new Set<string>();
  const out: PageRef[] = [];
  for (let i = stack.length - 1; i >= 0; i--) {
    const p = stack[i];
    if (seen.has(p.href)) continue;
    seen.add(p.href);
    out.push(p);
    if (out.length >= 10) break;
  }
  return out;
}
