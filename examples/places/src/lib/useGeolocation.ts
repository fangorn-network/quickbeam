import { useCallback, useEffect, useRef, useState } from 'react';

// Thin wrapper over the browser Geolocation API.
//
// `request()` calls getCurrentPosition, which prompts for permission the first
// time — so it must be tied to a user gesture (the map's "locate me" button).
// On mount we *silently* locate only when permission was already granted (probed
// via the Permissions API), so a returning user sees their dot without an
// unsolicited prompt, while a first-time visitor is never nagged on load.
export type GeoStatus = 'idle' | 'locating' | 'ok' | 'denied' | 'unavailable' | 'error';

export interface GeoState {
  position: [number, number] | null; // [lat, lng]
  status: GeoStatus;
}

export function useGeolocation(auto = true) {
  const [state, setState] = useState<GeoState>({ position: null, status: 'idle' });
  const busy = useRef(false);

  const request = useCallback(() => {
    if (busy.current) return;
    const geo = typeof navigator !== 'undefined' ? navigator.geolocation : undefined;
    if (!geo) {
      setState({ position: null, status: 'unavailable' });
      return;
    }
    busy.current = true;
    setState((s) => ({ ...s, status: 'locating' }));
    geo.getCurrentPosition(
      (pos) => {
        busy.current = false;
        setState({ position: [pos.coords.latitude, pos.coords.longitude], status: 'ok' });
      },
      (err) => {
        busy.current = false;
        setState({
          position: null,
          status: err.code === err.PERMISSION_DENIED ? 'denied' : 'error',
        });
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 },
    );
  }, []);

  useEffect(() => {
    if (!auto) return;
    const perms = (navigator as unknown as {
      permissions?: { query(d: { name: PermissionName }): Promise<{ state: string }> };
    }).permissions;
    if (!perms?.query) return; // can't tell silently — wait for an explicit request
    let cancelled = false;
    perms
      .query({ name: 'geolocation' as PermissionName })
      .then((st) => {
        if (!cancelled && st.state === 'granted') request();
      })
      .catch(() => {
        /* probing unsupported — leave it to the button */
      });
    return () => {
      cancelled = true;
    };
  }, [auto, request]);

  return { ...state, request };
}
