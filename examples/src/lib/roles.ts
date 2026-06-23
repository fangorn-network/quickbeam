// TypeScript twin of quickbeam/roles.py `infer_roles` — schema-agnostic semantic
// role inference. Given a sample of flattened records (field -> value), assign each
// field a semantic ROLE so the renderer can interpret *any* schema with no per-domain
// config. Mirrors the Python spec tables + algorithm exactly; keep the two in sync.
//
// When a domain manifest already carries a baked `role_map` (from `quickbeam cdn
// bake`), prefer it — this module is the fallback for a live Qdrant collection that
// has no manifest yet.

export type Role =
  | 'identity'
  | 'title'
  | 'subtitle'
  | 'temporal'
  | 'spatial'
  | 'media'
  | 'tags'
  | 'measures'
  | 'relations'
  | 'text';

export interface RoleMap {
  identity: string | null;
  title: string | null;
  subtitle: string | null;
  temporal: string | null;
  spatial: string | null;
  media: string | null;
  tags: string[];
  measures: string[];
  relations: string[];
  text: string[];
  labels: Record<string, string>;
  fields: string[];
}

const ROLE_SYNONYMS: Record<Role, string[]> = {
  identity: ['id', 'trackid', 'changesetid', 'contentid', 'uid', 'guid', 'key',
    'recordid', 'hash', 'leaf', 'nullifier'],
  title: ['title', 'name', 'label', 'headline', 'subject'],
  subtitle: ['byartist', 'artist', 'author', 'creator', 'user', 'userid', 'owner',
    'by', 'publisher', 'maker', 'contributor', 'channel'],
  tags: ['genres', 'genre', 'moods', 'mood', 'themes', 'theme', 'contexts', 'context',
    'tags', 'tag', 'categories', 'category', 'keywords', 'topics', 'labels'],
  temporal: ['datepublished', 'date', 'createdat', 'created', 'timestamp', 'time',
    'datetime', 'publishedat', 'updatedat', 'year', 'modified'],
  spatial: ['bbox', 'boundingbox', 'bounds', 'geo', 'location', 'coordinates',
    'latlon', 'lat', 'lon', 'lng', 'latitude', 'longitude', 'point', 'geometry', 'place'],
  measures: ['duration', 'durationms', 'length', 'count', 'numchanges', 'size',
    'score', 'amount', 'quantity', 'votes', 'plays', 'views', 'rank', 'weight'],
  media: ['audio', 'media', 'url', 'uri', 'handle', 'src', 'source', 'platformid',
    'videoid', 'contenturl', 'stream', 'embed', 'file', 'asset'],
  relations: ['album', 'albumname', 'release', 'releasegroup', 'group', 'collection',
    'parent', 'set', 'series', 'playlist'],
  text: ['description', 'comment', 'bio', 'notes', 'body', 'content', 'abstract',
    'review', 'summary', 'message'],
};

const SINGULAR_ROLES: Role[] = ['identity', 'title', 'subtitle', 'temporal', 'spatial', 'media'];
const MULTI_ROLES: Role[] = ['tags', 'measures', 'relations', 'text'];
const SINGULAR_PRIORITY: Role[] = ['identity', 'media', 'spatial', 'temporal', 'title', 'subtitle'];

const DENYLIST = new Set([
  'schemaversion', 'mbid', '_mbid', 'datacid', 'manifestcid', 'namehash',
  'blocktimestamp', 'version', 'owner',
]);

const STRONG = 3.0;
const WEAK = 1.5;
const THRESHOLD = 1.5;
const LONG_TEXT_CHARS = 120;

const DATE_RE = /^\s*\d{4}(-\d{2}(-\d{2})?)?([T ]\d{2}:\d{2})?/;
const URL_RE = /^(https?:|ipfs:|spotify:|[a-z]+:\/\/)/i;
const GEO_KEYS = new Set(['lat', 'lon', 'lng', 'latitude', 'longitude',
  'min_lon', 'min_lat', 'max_lon', 'max_lat', 'x', 'y']);

function norm(name: string): string {
  return String(name).toLowerCase().replace(/[^a-z0-9]/g, '');
}

function nameScore(normName: string, role: Role): number {
  let best = 0;
  for (const syn of ROLE_SYNONYMS[role]) {
    if (normName === syn) return STRONG;
    if (syn.length >= 4 && normName.length >= 4 && (normName.includes(syn) || syn.includes(normName)))
      best = Math.max(best, WEAK);
  }
  return best;
}

function looksDate(v: unknown): boolean {
  return typeof v === 'string' && DATE_RE.test(v);
}
function looksUrl(v: unknown): boolean {
  return typeof v === 'string' && URL_RE.test(v.trim());
}
function looksGeo(v: unknown): boolean {
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    return Object.keys(v as object).some((k) => GEO_KEYS.has(k.toLowerCase()));
  }
  return false;
}

interface Shape {
  array_of_str: boolean; number: boolean; date: boolean;
  geo: boolean; url: boolean; string: boolean; long_text: boolean;
}

function fieldShape(values: unknown[]): Shape {
  const n = values.length;
  const empty: Shape = { array_of_str: false, number: false, date: false, geo: false, url: false, string: false, long_text: false };
  if (n === 0) return empty;
  let arrayStr = 0, num = 0, date = 0, geo = 0, url = 0, str = 0, totalLen = 0, strCount = 0;
  for (const v of values) {
    if (Array.isArray(v)) {
      if (v.length && v.every((x) => typeof x === 'string')) arrayStr += 1;
    } else if (typeof v === 'boolean') {
      // ignore for measure inference
    } else if (typeof v === 'number') {
      num += 1;
    } else if (typeof v === 'string') {
      str += 1; strCount += 1; totalLen += v.length;
      if (looksDate(v)) date += 1;
      if (looksUrl(v)) url += 1;
    } else if (looksGeo(v)) {
      geo += 1;
    }
  }
  const avgLen = strCount ? totalLen / strCount : 0;
  const half = n / 2;
  return {
    array_of_str: arrayStr >= half && arrayStr > 0,
    number: num >= half && num > 0,
    date: date >= half && date > 0,
    geo: geo >= half && geo > 0,
    url: url >= half && url > 0,
    string: str >= half && str > 0,
    long_text: avgLen >= LONG_TEXT_CHARS,
  };
}

const TYPE_BONUS: Partial<Record<Role, [keyof Shape, number]>> = {
  tags: ['array_of_str', 2.0],
  measures: ['number', 1.5],
  temporal: ['date', 2.0],
  spatial: ['geo', 2.5],
  media: ['url', 1.5],
  text: ['long_text', 2.0],
};

function roleScores(normName: string, shape: Shape): Partial<Record<Role, number>> {
  const scores: Partial<Record<Role, number>> = {};
  (Object.keys(ROLE_SYNONYMS) as Role[]).forEach((role) => {
    let s = nameScore(normName, role);
    const bonus = TYPE_BONUS[role];
    if (bonus && shape[bonus[0]]) s += bonus[1];
    if ((role === 'identity' || role === 'title' || role === 'subtitle') && shape.string && !shape.long_text)
      s += 0.5;
    if (s > 0) scores[role] = s;
  });
  return scores;
}

const SINGULARIZE: [string, string][] = [['ies', 'y'], ['ses', 's'], ['s', '']];

export function fieldLabel(name: string, singular = false): string {
  if (!name) return '';
  let raw = name.replace(/^by(?=[A-Z_])/, '');
  raw = raw.replace(/(_id|Id)$/, '') || raw;
  let parts = raw.replace(/(?<=[a-z0-9])(?=[A-Z])/g, ' ').replace(/[_-]+/g, ' ').split(/\s+/).filter(Boolean);
  if (parts.length === 0) parts = [name];
  if (singular) {
    const last = parts[parts.length - 1].toLowerCase();
    for (const [suf, rep] of SINGULARIZE) {
      if (last.endsWith(suf) && last.length > suf.length + 1) {
        const w = parts[parts.length - 1];
        parts[parts.length - 1] = w.slice(0, w.length - suf.length) + rep;
        break;
      }
    }
  }
  return parts.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

export function inferRoles(records: Array<Record<string, unknown>>): RoleMap {
  const valuesByField: Record<string, unknown[]> = {};
  for (const rec of records.slice(0, 500)) {
    if (!rec || typeof rec !== 'object') continue;
    for (const [k, v] of Object.entries(rec)) {
      if (v == null) continue;
      (valuesByField[k] ??= []);
      if (valuesByField[k].length < 50) valuesByField[k].push(v);
    }
  }

  const fields = Object.keys(valuesByField).sort();
  const shapes: Record<string, Shape> = {};
  const scores: Record<string, Partial<Record<Role, number>>> = {};
  for (const f of fields) {
    if (DENYLIST.has(norm(f))) continue;
    shapes[f] = fieldShape(valuesByField[f]);
    scores[f] = roleScores(norm(f), shapes[f]);
  }

  const roleMap: RoleMap = {
    identity: null, title: null, subtitle: null, temporal: null, spatial: null,
    media: null, tags: [], measures: [], relations: [], text: [], labels: {}, fields,
  };
  const used = new Set<string>();

  // Tags first — array-of-string facets are unambiguous.
  for (const f of fields) {
    if (used.has(f) || !scores[f]) continue;
    if (shapes[f].array_of_str && (scores[f].tags ?? 0) >= THRESHOLD) {
      roleMap.tags.push(f); used.add(f);
    }
  }
  const distinct = (field: string): number => {
    const seen = new Set<string>();
    for (const v of valuesByField[field] ?? []) {
      if (Array.isArray(v)) v.forEach((x) => seen.add(String(x)));
    }
    return seen.size;
  };
  roleMap.tags.sort((a, b) => distinct(b) - distinct(a) || a.localeCompare(b));

  // Singular roles, priority order, each claiming its best free field.
  for (const role of SINGULAR_PRIORITY) {
    let bestF: string | null = null;
    let bestS = THRESHOLD - 0.001;
    for (const f of fields) {
      if (used.has(f) || !scores[f]) continue;
      const s = scores[f][role] ?? 0;
      if (s > bestS) { bestF = f; bestS = s; }
    }
    if (bestF) { (roleMap[role] as string | null) = bestF; used.add(bestF); }
  }

  // Multi roles over the remainder.
  for (const f of fields) {
    if (used.has(f) || !scores[f]) continue;
    const sc = scores[f];
    if (shapes[f].number && (sc.measures ?? 0) >= THRESHOLD) { roleMap.measures.push(f); used.add(f); continue; }
    if ((sc.relations ?? 0) >= STRONG) { roleMap.relations.push(f); used.add(f); continue; }
    if (shapes[f].long_text || (sc.text ?? 0) >= STRONG) { roleMap.text.push(f); used.add(f); continue; }
  }

  for (const role of SINGULAR_ROLES) {
    const f = roleMap[role] as string | null;
    if (f) roleMap.labels[role] = fieldLabel(f, true);
  }
  for (const role of MULTI_ROLES) {
    const arr = roleMap[role] as string[];
    if (arr.length) roleMap.labels[role] = arr.map((f) => fieldLabel(f)).join(', ');
  }

  return roleMap;
}
