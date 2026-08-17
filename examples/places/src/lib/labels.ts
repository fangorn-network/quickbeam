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

// Some scalar string fields hold a JSON-encoded array (e.g. amenities:
// '["live music","dine-in"]'). Parse those back into items; return null if the
// value isn't a JSON array so callers can fall through to plain rendering.
export function parseJsonArray(v: unknown): string[] | null {
  if (typeof v !== 'string') return null;
  const t = v.trim();
  if (!t.startsWith('[')) return null;
  try {
    const a = JSON.parse(t);
    return Array.isArray(a) ? a.map(String) : null;
  } catch {
    return null;
  }
}

const DAY_NAMES = [
  'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
];

// Inclusive [from, to] ISO-date (YYYY-MM-DD) bounds for an event date window,
// in the viewer's local time. ISO date strings compare correctly with `<`/`>`,
// so callers can test `startDate.slice(0,10)` directly against these.
//   today   — just today
//   weekend — the coming Sat–Sun (or the remaining weekend if it's already Sat/Sun)
//   week    — today through the coming Sunday
export function dateWindowBounds(w: 'today' | 'weekend' | 'week'): { from: string; to: string } {
  const now = new Date();
  const iso = (d: Date) => {
    // Local-date ISO (avoid UTC shift from toISOString()).
    const off = d.getTimezoneOffset() * 60000;
    return new Date(d.getTime() - off).toISOString().slice(0, 10);
  };
  const day = now.getDay(); // 0 Sun … 6 Sat
  const today = iso(now);
  if (w === 'today') return { from: today, to: today };
  if (w === 'week') {
    const end = new Date(now);
    end.setDate(now.getDate() + ((7 - day) % 7)); // through the coming Sunday
    return { from: today, to: iso(end) };
  }
  // weekend
  const toSat = (6 - day + 7) % 7;
  const sat = new Date(now);
  sat.setDate(now.getDate() + toSat);
  const sun = new Date(sat);
  sun.setDate(sat.getDate() + 1);
  if (day === 0) return { from: today, to: today }; // Sunday: just today
  if (day === 6) return { from: today, to: iso(sun) }; // Saturday: today + Sunday
  return { from: iso(sat), to: iso(sun) };
}

// Parse a clock time like "11:00 AM" / "2 PM" (tolerating the narrow/no-break
// spaces Google uses) into minutes since midnight. Null if unparseable.
function timeToMinutes(s: string): number | null {
  const m = s.match(/(\d{1,2})(?::(\d{2}))?\s*([AP])M/i);
  if (!m) return null;
  let h = parseInt(m[1], 10) % 12;
  if (m[3].toUpperCase() === 'P') h += 12;
  return h * 60 + parseInt(m[2] ?? '0', 10);
}

// Decide whether a place is open right now from parsed hours rows, using the
// viewer's local clock (a demo simplification — we don't know the place's TZ).
// Returns null when hours can't be interpreted. Handles "Closed", "Open 24
// hours", multiple ranges per day, and overnight ranges that cross midnight.
export function isOpenNow(rows: { day: string; hours: string }[] | null): boolean | null {
  if (!rows || rows.length === 0) return null;
  const now = new Date();
  const today = now.getDay();
  const nowMin = now.getHours() * 60 + now.getMinutes();

  const byDay = new Map<number, { start: number; end: number }[]>();
  let parsedAny = false;
  for (const r of rows) {
    const di = DAY_NAMES.indexOf(r.day.trim());
    if (di < 0) continue;
    const norm = r.hours.replace(/[   ]/g, ' ').replace(/[–—]/g, '-');
    if (/closed/i.test(norm)) { parsedAny = true; continue; }
    if (/24\s*hours/i.test(norm)) { byDay.set(di, [{ start: 0, end: 1440 }]); parsedAny = true; continue; }
    for (const part of norm.split(',')) {
      const dash = part.indexOf('-');
      if (dash < 0) continue;
      const start = timeToMinutes(part.slice(0, dash));
      const end = timeToMinutes(part.slice(dash + 1));
      if (start == null || end == null) continue;
      parsedAny = true;
      if (!byDay.has(di)) byDay.set(di, []);
      byDay.get(di)!.push({ start, end });
    }
  }
  if (!parsedAny) return null;

  for (const { start, end } of byDay.get(today) ?? []) {
    if (end > start ? nowMin >= start && nowMin < end : nowMin >= start) return true;
  }
  // A range from yesterday that crosses midnight may still be open this morning.
  for (const { start, end } of byDay.get((today + 6) % 7) ?? []) {
    if (end <= start && nowMin < end) return true;
  }
  return false;
}

// Parse a Google-style opening-hours string ("Monday: 9 AM – 5 PM; Tuesday:
// Closed; …") into day/hours rows. Returns null if it doesn't look like one.
export function parseHours(v: unknown): { day: string; hours: string }[] | null {
  if (typeof v !== 'string' || !v.includes(':')) return null;
  const rows = v
    .split(';')
    .map((part) => {
      const i = part.indexOf(':');
      if (i < 0) return null;
      const day = part.slice(0, i).trim();
      const hours = part.slice(i + 1).trim();
      return day && hours ? { day, hours } : null;
    })
    .filter((r): r is { day: string; hours: string } => r !== null);
  return rows.length ? rows : null;
}
