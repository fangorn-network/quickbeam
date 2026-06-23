// Generic, domain-agnostic display helpers. Per-domain label maps, field ordering,
// and external-URL patterns now live in the Domain (lib/domain.ts), driven by the
// inferred role map + optional presentation overlay.

// Humanise a raw key/rel: hyphens/underscores → spaces, split camelCase, title-case.
export function humanise(raw: string): string {
  const spaced = raw
    .replace(/[-_]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2');
  return spaced
    .split(/\s+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

export function formatDuration(ms: number): string {
  const totalSec = Math.round(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function formatRating(r: number): string {
  return `${r.toFixed(1)} / 5`;
}

export function splitList(v: string): string[] {
  return v
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}
