// Small view helpers shared across pages.
import type { Point } from './types';

export const entityHref = (id: string) => `/entity?id=${encodeURIComponent(id)}`;
export const searchHref = (q: string) => `/search?q=${encodeURIComponent(q)}`;

const SEP = ' › ';

// The readable body of a node: strip the leading "breadcrumb — " prefix (shown
// separately) and the trailing " Heard in patches: …" back-fill (shown as a rail).
export function bodyText(p: Point): string {
  let t = String(p.fields.text ?? '');
  const bf = t.indexOf(' Heard in patches:');
  if (bf >= 0) t = t.slice(0, bf);
  const crumb = p.fields.breadcrumb ? String(p.fields.breadcrumb) + ' — ' : '';
  if (crumb && t.startsWith(crumb)) t = t.slice(crumb.length);
  return t.trim();
}

export function crumbParts(p: Point): string[] {
  return p.fields.breadcrumb ? String(p.fields.breadcrumb).split(SEP) : [];
}

export function snippet(p: Point, n = 170): string {
  const t = bodyText(p);
  return t.length > n ? t.slice(0, n).replace(/\s+\S*$/, '') + '…' : t;
}

// A parameter's description = its text minus the "name — ", "Range: …", and
// "(Parameter of X.)" scaffolding (those are shown in their own columns).
export function paramDesc(p: Point): string {
  let t = String(p.fields.text ?? '');
  const name = String(p.fields.title ?? '');
  if (t.startsWith(name + ' — ')) t = t.slice((name + ' — ').length);
  return t
    .replace(/\s*Range:[^.]*\.\s*/, ' ')
    .replace(/\s*\(Parameter of[^)]*\.\)\s*/, '')
    .trim();
}
