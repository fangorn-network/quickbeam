// Geographic helpers for coordinate-proximity ("find nearby") search.

// Parse a "lat,lng" string into a coordinate pair, or null. Rejects out-of-range
// values: a stray "200,300" is finite but bogus, and feeding it to the map throws
// ("Invalid LngLat") or corrupts the camera, so it must never reach a feature.
export function parseCoords(v: unknown): [number, number] | null {
  if (typeof v !== 'string') return null;
  const parts = v.split(',');
  if (parts.length !== 2) return null;
  const lat = Number(parts[0]);
  const lng = Number(parts[1]);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return null;
  return [lat, lng];
}

// Great-circle distance (km) between two coordinate pairs.
export function haversineKm(
  [lat1, lng1]: [number, number],
  [lat2, lng2]: [number, number],
): number {
  const R = 6371;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}
