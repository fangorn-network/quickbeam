// Figure delivery, privacy-preserving. Instead of a per-figure <img> request (which
// would let the host see which sections a visitor opens), the whole figure set is
// downloaded ONCE as a single bundle (domains/<d>/images.json = {file: base64}) and
// each figure is turned into an in-memory blob: URL. Rendering a figure then reads
// from memory — no network — so browsing reveals nothing; the host sees exactly one
// identical bundle fetch for every visitor.
import { CDN_URL, CDN_DOMAIN } from './config';
import { useAsync } from './useAsync';

async function load(): Promise<Map<string, string>> {
  const map = new Map<string, string>();
  try {
    const res = await fetch(`${CDN_URL}/domains/${CDN_DOMAIN}/images.json`);
    if (!res.ok) return map;
    const obj = (await res.json()) as Record<string, string>;
    for (const [file, b64] of Object.entries(obj)) {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      map.set(file, URL.createObjectURL(new Blob([bytes], { type: 'image/png' })));
    }
  } catch {
    /* no bundle → figures just don't render */
  }
  return map;
}

let _bundle: Promise<Map<string, string>> | null = null;
// One fetch + decode per session (memoized). Call early (App) to prefetch.
export function imageBundle(): Promise<Map<string, string>> {
  return (_bundle ??= load());
}

// file → blob: URL, or null while the bundle is still downloading.
export function useImages(): Map<string, string> | null {
  return useAsync(() => imageBundle(), []).data;
}
