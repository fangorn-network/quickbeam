// Bento shelves — turn the Atlas point set into a navigable grid of cards.
//
// The dot cloud is gone; we group entities into shelves and lay each shelf out as a
// bento grid (varied tile sizes, real tap targets). Two grouping axes:
//
//   'place' — shelves are geographic. `locality` strings are too dirty to group on
//     (a town appears as "Eagle River" / "Eagle River, WI" / "Eagle River,
//     Wisconsin", and 284 businesses carry only "WI"), so we GROUP by coordinates
//     and only LABEL with the cleaned city. Items with a clean city but no coords
//     still group by city; items with coords but a dirty/absent city snap to the
//     nearest city centroid; the rest land in a semantic "No fixed location" shelf.
//   'vibe'  — shelves are semantic: grouped by category, ignoring location.
//
// Within every shelf, tiles are ORDERED by the UMAP/PCA x-coordinate so adjacent
// cards are semantically related — that's the job the projection is actually good
// at. Tile SIZE comes from a prominence weight (rating × popularity, recency).
import type { AtlasPoint } from './atlas';
import { parseCoords, haversineKm } from './geo';

export type BentoMode = 'place' | 'vibe';
export type TileSize = 'hero' | 'lg' | 'md' | 'sm';

export interface Tile {
  point: AtlasPoint;
  size: TileSize;
  score?: number; // query relevance, when a search is active
}

export interface Shelf {
  key: string;
  label: string;
  sublabel?: string;
  tiles: Tile[];
}

const NO_LOCATION = '__nowhere__';
const OTHER_AREA = '__other__';
const MAX_SNAP_KM = 60; // a coord-only point snaps to a city centroid within this

// Drop a state/county suffix and reject bare state tokens → a clean city label.
const STATE_RE = /^(wi|wisconsin|mi|michigan|mn|minnesota|il|illinois|ia|iowa|usa|us)$/i;
export function canonCity(loc: unknown): string | null {
  if (typeof loc !== 'string') return null;
  const s = loc.trim();
  if (!s) return null;
  const comma = s.indexOf(',');
  const city = (comma >= 0 ? s.slice(0, comma) : s).trim();
  if (city.length < 2 || STATE_RE.test(city)) return null;
  return city;
}

// ---- prominence weight → drives tile size ----
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}
function weight(p: AtlasPoint): number {
  const f = p.fields;
  if (p.type === 'Event') {
    // Upcoming events score higher; past ones sink. Recency within the future window.
    const past = f.isPast === true;
    const hasImg = typeof f.imageUrl === 'string' && f.imageUrl ? 1 : 0;
    return (past ? 0.2 : 1) + hasImg * 0.5;
  }
  const rating = num(f.rating) ?? 0;
  const count = num(f.userRatingCount) ?? 0;
  return rating * Math.log10(count + 10); // rating weighted by review volume
}

// Assign tile sizes from per-shelf weight ranking, then order by UMAP adjacency.
function buildTiles(points: AtlasPoint[], scores?: Map<string, number>): Tile[] {
  const byWeight = [...points].sort((a, b) => weight(b) - weight(a));
  const size = new Map<string, TileSize>();
  byWeight.forEach((p, i) => {
    let s: TileSize = 'md';
    if (i === 0) s = 'hero';
    else if (i <= 2) s = 'lg';
    else if (i >= byWeight.length * 0.65) s = 'sm';
    size.set(p.id, s);
  });
  // Order along the shelf by the semantic x so neighbors are related.
  return [...points]
    .sort((a, b) => a.x - b.x)
    .map((p) => ({ point: p, size: size.get(p.id) ?? 'md', score: scores?.get(p.id) }));
}

// ---- place grouping ----
function groupByPlace(points: AtlasPoint[]): Shelf[] {
  // 1. Clean city per point + collect centroids from points that have both.
  const cityOf = new Map<string, string | null>();
  const sum = new Map<string, { lat: number; lng: number; n: number }>();
  for (const p of points) {
    const city = canonCity(p.fields.locality);
    cityOf.set(p.id, city);
    const c = parseCoords(p.fields.coordinates);
    if (city && c) {
      const acc = sum.get(city) ?? { lat: 0, lng: 0, n: 0 };
      acc.lat += c[0];
      acc.lng += c[1];
      acc.n += 1;
      sum.set(city, acc);
    }
  }
  const centroids = [...sum.entries()].map(([city, a]) => ({
    city,
    lat: a.lat / a.n,
    lng: a.lng / a.n,
  }));

  // 2. Assign each point to a shelf key.
  const groups = new Map<string, AtlasPoint[]>();
  const push = (k: string, p: AtlasPoint) => {
    const arr = groups.get(k) ?? [];
    arr.push(p);
    groups.set(k, arr);
  };
  for (const p of points) {
    const city = cityOf.get(p.id) ?? null;
    if (city) {
      push(city, p);
      continue;
    }
    const c = parseCoords(p.fields.coordinates);
    if (c && centroids.length) {
      let best = centroids[0];
      let bestD = Infinity;
      for (const ct of centroids) {
        const d = haversineKm(c, [ct.lat, ct.lng]);
        if (d < bestD) {
          bestD = d;
          best = ct;
        }
      }
      push(bestD <= MAX_SNAP_KM ? best.city : OTHER_AREA, p);
    } else {
      push(NO_LOCATION, p);
    }
  }

  return finishShelves(groups, {
    [OTHER_AREA]: 'Other areas',
    [NO_LOCATION]: 'No fixed location',
  });
}

// ---- vibe grouping (semantic, location-free) ----
function categoryOf(p: AtlasPoint): string {
  const f = p.fields;
  const cats = Array.isArray(f.categories) ? f.categories.map(String) : [];
  const primary =
    (typeof f.primaryType === 'string' && f.primaryType) || cats[0] || (p.type === 'Event' ? 'event' : 'place');
  return primary.replace(/_/g, ' ');
}
function groupByVibe(points: AtlasPoint[]): Shelf[] {
  const groups = new Map<string, AtlasPoint[]>();
  for (const p of points) {
    const k = categoryOf(p).toLowerCase();
    const arr = groups.get(k) ?? [];
    arr.push(p);
    groups.set(k, arr);
  }
  return finishShelves(groups, {});
}

// Title-case a shelf key into a label unless it's a known sentinel.
const SENTINEL_LABELS: Record<string, string> = {
  [OTHER_AREA]: 'Other areas',
  [NO_LOCATION]: 'No fixed location',
};
function labelFor(key: string, overrides: Record<string, string>): string {
  return overrides[key] ?? SENTINEL_LABELS[key] ?? key.replace(/\b\w/g, (c) => c.toUpperCase());
}

const MIN_SHELF = 3; // shelves smaller than this fold into a catch-all

function finishShelves(groups: Map<string, AtlasPoint[]>, overrides: Record<string, string>): Shelf[] {
  // Fold tiny shelves into "Other areas" so the page isn't a long tail of
  // single-tile sections. (The no-location shelf is never folded.)
  const overflow: AtlasPoint[] = [...(groups.get(OTHER_AREA) ?? [])];
  for (const [key, pts] of [...groups.entries()]) {
    if (key === OTHER_AREA || key === NO_LOCATION) continue;
    if (pts.length < MIN_SHELF) {
      overflow.push(...pts);
      groups.delete(key);
    }
  }
  if (overflow.length) groups.set(OTHER_AREA, overflow);

  const shelves: Shelf[] = [...groups.entries()].map(([key, pts]) => ({
    key,
    label: labelFor(key, overrides),
    sublabel: `${pts.length} ${pts.length === 1 ? 'place' : 'places'}`,
    tiles: buildTiles(pts),
  }));
  // Biggest shelves first, but always sink the catch-all/no-location shelves.
  const sink = new Set([OTHER_AREA, NO_LOCATION]);
  shelves.sort((a, b) => {
    const as = sink.has(a.key) ? 1 : 0;
    const bs = sink.has(b.key) ? 1 : 0;
    if (as !== bs) return as - bs;
    return b.tiles.length - a.tiles.length;
  });
  return shelves;
}

export function buildShelves(points: AtlasPoint[], mode: BentoMode): Shelf[] {
  return mode === 'place' ? groupByPlace(points) : groupByVibe(points);
}

// A "Top matches" shelf for an active query: the top-N semantic neighbors as tiles,
// ranked (hero = best), pulled across all shelves.
export function topMatchesShelf(
  points: AtlasPoint[],
  neighbors: Array<{ id: string; score: number }>,
): Shelf | null {
  if (!neighbors.length) return null;
  const byId = new Map(points.map((p) => [p.id, p]));
  const tiles: Tile[] = [];
  neighbors.forEach((n, i) => {
    const point = byId.get(n.id);
    if (!point) return;
    const size: TileSize = i === 0 ? 'hero' : i <= 2 ? 'lg' : i >= neighbors.length * 0.65 ? 'sm' : 'md';
    tiles.push({ point, size, score: n.score });
  });
  return { key: '__top__', label: 'Top matches', tiles };
}
