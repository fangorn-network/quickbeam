// Geographic map of entities. Plots every result that carries a "lat,lng"
// `coordinates` field as a colored dot (by type accent), entirely from free,
// keyless Carto raster tiles + OpenStreetMap data. A GeoJSON circle layer (rather
// than one DOM marker per point) keeps it smooth at the thousands of trails/lakes/
// landmarks the OSM graph now carries. Clicking a dot opens the entity; the active
// result is ringed so the list and map stay in sync.
import { useEffect, useMemo, useRef } from 'react';
import maplibregl from 'maplibre-gl';
import type { GeoJSONSource, StyleSpecification } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { EntitySummary } from '../lib/types';
import { parseCoords, haversineKm } from '../lib/geo';
import { COMMUNITY } from '../lib/i18n';
import { useGeolocation } from '../lib/useGeolocation';
import styles from './MapView.module.css';

// Fallback center when nothing geolocates yet — comes from the active locale
// (e.g. Bishop Arts, Dallas), defaulting to Eagle River, WI for legacy profiles.
const FALLBACK_CENTER: [number, number] = COMMUNITY.center ?? [-89.2446, 45.9172];

// How close the device must be to the dataset before we plot "you are here". A
// SOND3R deployment is one neighborhood/metro; a user hundreds of km away (viewing
// Oak Cliff from another state) gets no dot — a pin off in the next region is noise,
// not a landmark. ~100 km comfortably covers a metro while excluding other cities.
const GEO_NEAR_KM = 100;

// Keyless Carto raster basemap, light or dark to match the app theme.
function rasterStyle(dark: boolean): StyleSpecification {
  const variant = dark ? 'dark_all' : 'voyager';
  return {
    version: 8,
    sources: {
      carto: {
        type: 'raster',
        tiles: ['a', 'b', 'c', 'd'].map(
          (s) => `https://${s}.basemaps.cartocdn.com/rastertiles/${variant}/{z}/{x}/{y}{r}.png`,
        ),
        tileSize: 256,
        attribution: '© OpenStreetMap © CARTO',
      },
    },
    layers: [{ id: 'carto', type: 'raster', source: 'carto' }],
  };
}

function isDark(): boolean {
  return document.documentElement.getAttribute('data-theme') === 'dark';
}

interface Props {
  items: EntitySummary[];
  accentOf: (type: string) => string;
  onOpen: (e: EntitySummary) => void;
  activeId?: string | null;
  onHover?: (id: string | null) => void;
  // When set, fly to this entity's pin (arriving from its own page).
  focusId?: string | null;
}

function toFeatures(items: EntitySummary[], accentOf: (t: string) => string) {
  const features: GeoJSON.Feature[] = [];
  for (const e of items) {
    const c = parseCoords(e.fields?.coordinates);
    if (!c) continue;
    features.push({
      type: 'Feature',
      // parseCoords yields [lat, lng]; GeoJSON wants [lng, lat].
      geometry: { type: 'Point', coordinates: [c[1], c[0]] },
      properties: { id: e.pointId, title: e.title, accent: accentOf(e.entityType) },
    });
  }
  return { type: 'FeatureCollection' as const, features };
}

export default function MapView({ items, accentOf, onOpen, activeId, onHover, focusId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const readyRef = useRef(false);
  // Keep the latest items/handlers reachable from stable map event callbacks.
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  const onHoverRef = useRef(onHover);
  onHoverRef.current = onHover;

  // ---- GPS: "you are here", shown only when the device is near the dataset ----
  const geo = useGeolocation(true);
  // Distance anchor: the locale's center ([lng,lat]) if set, else the centroid of
  // whatever's currently plotted — so the near/far test works for every locale.
  const anchor = useMemo<[number, number] | null>(() => {
    if (COMMUNITY.center) return [COMMUNITY.center[1], COMMUNITY.center[0]]; // [lat,lng]
    const pts = items.map((e) => parseCoords(e.fields?.coordinates)).filter(Boolean) as [number, number][];
    if (!pts.length) return null;
    const [sLat, sLng] = pts.reduce(([a, b], [la, ln]) => [a + la, b + ln], [0, 0]);
    return [sLat / pts.length, sLng / pts.length];
  }, [items]);

  const distanceKm = geo.position && anchor ? haversineKm(geo.position, anchor) : null;
  const isFar = distanceKm != null && distanceKm > GEO_NEAR_KM;
  // Only plot the user when we have a fix AND it's near enough to be useful.
  const userPoint = geo.position && !isFar ? geo.position : null;

  // A user-initiated "locate me" should recenter; a silent auto-locate must not
  // yank the camera. This flag, set by the button, gates the one-time ease-to.
  const recenterRef = useRef(false);
  const locate = () => {
    recenterRef.current = true;
    geo.request();
  };
  // Reachable from the stable map callbacks so a theme-driven style swap (which
  // clears + re-installs sources) can re-seed the dot instead of dropping it.
  const userPointRef = useRef(userPoint);
  userPointRef.current = userPoint;

  // Create the map once.
  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: rasterStyle(isDark()),
      center: FALLBACK_CENTER,
      zoom: 9,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

    // (Re)add the entity source + circle layers. Called on first load and again
    // after a theme-driven style swap (which clears sources/layers but keeps our
    // layer-scoped event handlers, which rebind by id).
    const installLayers = () => {
      if (!map.getSource('entities')) {
        map.addSource('entities', { type: 'geojson', data: toFeatures(itemsRef.current, accentOf) });
      }
      // Ring drawn *under* the active point (filtered to nothing until set).
      if (!map.getLayer('pts-active')) {
        map.addLayer({
          id: 'pts-active',
          type: 'circle',
          source: 'entities',
          filter: ['==', ['get', 'id'], '__none__'],
          paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 9, 12, 13, 16, 18],
            'circle-color': ['get', 'accent'],
            'circle-opacity': 0.25,
            'circle-stroke-width': 2,
            'circle-stroke-color': ['get', 'accent'],
          },
        });
      }
      if (!map.getLayer('pts')) {
        map.addLayer({
          id: 'pts',
          type: 'circle',
          source: 'entities',
          paint: {
            'circle-color': ['get', 'accent'],
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 4, 12, 7, 16, 11],
            'circle-stroke-width': 1.5,
            'circle-stroke-color': isDark() ? '#0b0e14' : '#ffffff',
            'circle-opacity': 0.9,
          },
        });
      }
      // "You are here" — a distinct blue dot + soft halo, on top of the entity
      // pins. Seeded from the current fix (via ref) so it survives a style swap.
      if (!map.getSource('me')) {
        map.addSource('me', { type: 'geojson', data: meFeatures(userPointRef.current) });
      }
      if (!map.getLayer('me-halo')) {
        map.addLayer({
          id: 'me-halo',
          type: 'circle',
          source: 'me',
          paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 12, 12, 22, 16, 34],
            'circle-color': '#1a73e8',
            'circle-opacity': 0.15,
          },
        });
      }
      if (!map.getLayer('me-dot')) {
        map.addLayer({
          id: 'me-dot',
          type: 'circle',
          source: 'me',
          paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 5, 12, 7, 16, 9],
            'circle-color': '#1a73e8',
            'circle-stroke-width': 2.5,
            'circle-stroke-color': '#ffffff',
          },
        });
      }
    };

    const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
    map.on('mouseenter', 'pts', (ev) => {
      map.getCanvas().style.cursor = 'pointer';
      const f = ev.features?.[0];
      const id = f?.properties?.id as string | undefined;
      if (f && id) {
        const [lng, lat] = (f.geometry as GeoJSON.Point).coordinates as [number, number];
        pop.setLngLat([lng, lat]).setText(String(f.properties?.title ?? '')).addTo(map);
        onHoverRef.current?.(id);
      }
    });
    map.on('mouseleave', 'pts', () => {
      map.getCanvas().style.cursor = '';
      pop.remove();
      onHoverRef.current?.(null);
    });
    map.on('click', 'pts', (ev) => {
      const id = ev.features?.[0]?.properties?.id as string | undefined;
      if (!id) return;
      const hit = itemsRef.current.find((x) => x.pointId === id);
      if (hit) onOpenRef.current(hit);
    });

    map.on('load', () => {
      installLayers();
      readyRef.current = true;
      fitTo(map, toFeatures(itemsRef.current, accentOf));
    });

    // Swap basemap when the app theme toggles; re-install our layers afterward.
    const obs = new MutationObserver(() => {
      readyRef.current = false;
      map.setStyle(rasterStyle(isDark()));
      map.once('idle', () => {
        installLayers();
        readyRef.current = true;
      });
    });
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    return () => {
      obs.disconnect();
      map.remove();
      mapRef.current = null;
      readyRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Push new data + refit whenever the result set changes.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    const data = toFeatures(items, accentOf);
    const src = map.getSource('entities') as GeoJSONSource | undefined;
    if (src) {
      src.setData(data);
      // When focusing a single pin, the fly-to effect frames the view instead.
      if (!focusId) fitTo(map, data);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  // Ring the active result.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current || !map.getLayer('pts-active')) return;
    map.setFilter('pts-active', ['==', ['get', 'id'], activeId ?? '__none__']);
  }, [activeId]);

  // Plot / clear "you are here". On a user-initiated locate (recenterRef) we ease
  // to the dot; a silent auto-locate just drops it without moving the camera.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    const src = map.getSource('me') as GeoJSONSource | undefined;
    if (!src) return;
    src.setData(meFeatures(userPoint));
    if (userPoint && recenterRef.current) {
      recenterRef.current = false;
      map.easeTo({ center: [userPoint[1], userPoint[0]], zoom: Math.max(13, map.getZoom()), duration: 700 });
    } else if (!userPoint) {
      recenterRef.current = false; // a far/denied fix shouldn't recenter a later one
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userPoint?.[0], userPoint?.[1]]);

  // Fly to a focused entity once it's present in the data (it may load a tick
  // after the route does). Track the last id flown so re-renders don't re-fly.
  const flownRef = useRef<string | null>(null);
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !focusId) {
      flownRef.current = focusId ?? null;
      return;
    }
    if (flownRef.current === focusId) return;
    const hit = items.find((e) => e.pointId === focusId);
    const c = hit ? parseCoords(hit.fields?.coordinates) : null;
    if (!c) return; // not loaded yet — a later items update will retry
    const doFly = () => map.flyTo({ center: [c[1], c[0]], zoom: 14, duration: 900 });
    if (readyRef.current) doFly();
    else map.once('load', doFly);
    flownRef.current = focusId;
  }, [focusId, items]);

  return (
    <div className={styles.wrap}>
      <div ref={containerRef} className={styles.map} />
      {geo.status !== 'unavailable' && (
        <button
          type="button"
          className={`${styles.locate} ${userPoint ? styles.locateOn : ''}`}
          onClick={locate}
          disabled={geo.status === 'locating'}
          aria-label="Show my location on the map"
          title={
            geo.status === 'denied'
              ? 'Location permission is blocked'
              : isFar && distanceKm != null
                ? `You're ~${Math.round(distanceKm)} km from ${COMMUNITY.name} — showing the area`
                : 'Show my location'
          }
        >
          {geo.status === 'locating' ? (
            <span className={styles.locateSpin} aria-hidden="true" />
          ) : (
            <span aria-hidden="true">◎</span>
          )}
        </button>
      )}
      {isFar && distanceKm != null && (
        <div className={styles.farNote}>You’re ~{Math.round(distanceKm)} km from {COMMUNITY.name}</div>
      )}
    </div>
  );
}

// The "you are here" source data: a single Point at [lng,lat], or empty when we
// have no usable fix. `pt` is [lat,lng] (geolocation order).
function meFeatures(pt: [number, number] | null): GeoJSON.FeatureCollection {
  if (!pt) return { type: 'FeatureCollection', features: [] };
  return {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [pt[1], pt[0]] } }],
  };
}

function fitTo(map: maplibregl.Map, data: GeoJSON.FeatureCollection) {
  if (!data.features.length) return;
  const b = new maplibregl.LngLatBounds();
  let n = 0;
  for (const f of data.features) {
    const [lng, lat] = (f.geometry as GeoJSON.Point).coordinates as [number, number];
    if (!Number.isFinite(lng) || !Number.isFinite(lat)) continue;
    b.extend([lng, lat]);
    n += 1;
  }
  if (!n) return;
  // A single pin (or several at identical coordinates) gives a zero-area box, and
  // fitBounds on that computes an infinite zoom → NaN camera → WebGL crash (white
  // screen). Center on it at a sensible zoom instead. This is the common case now
  // that "hidden gems" / focus can narrow the map to one plottable result.
  const sw = b.getSouthWest();
  const ne = b.getNorthEast();
  if (sw.lng === ne.lng && sw.lat === ne.lat) {
    map.easeTo({ center: [sw.lng, sw.lat], zoom: Math.max(12, map.getZoom()), duration: 600 });
    return;
  }
  map.fitBounds(b, { padding: 56, maxZoom: 14, duration: 600 });
}
