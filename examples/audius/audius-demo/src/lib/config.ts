const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};

// The snapshot, fetched SAME-ORIGIN by default: vite proxies /cdn to wherever
// `quickbeam cdn serve` is listening (see vite.config.ts).
//
// Naming the CDN absolutely — http://localhost:8090 — only ever works on the machine
// running it. Through a tunnel `localhost` is the visitor's own machine, and an
// http:// fetch from an https:// page is blocked as mixed content anyway. A relative
// path follows whatever origin the page was served from, so the same build works
// locally, over ngrok, and behind any reverse proxy.
//
// Set VITE_CDN_URL to an absolute URL only if the CDN is genuinely served from
// somewhere else (and is reachable over https from the visitor).
export const CDN_URL = (env.VITE_CDN_URL ?? '/cdn').replace(/\/$/, '');

/** Empty → take the first domain in the catalog. */
export const CDN_DOMAIN = env.VITE_DOMAIN ?? 'audius';

// Artwork is stored in the graph as a CID, never a URL — the Audius API hands back a
// different content-node host per response and they all serve byte-identical bytes.
// This is the node we resolve those CIDs through.
export const CONTENT_NODE = (env.VITE_CONTENT_NODE ?? 'https://creatornode.audius.co').replace(/\/$/, '');

// A *discovery* node — distinct from CONTENT_NODE above. `/v1/tracks/{id}/stream`
// 302s from here to whichever content node holds the audio, with an open CORS header
// and byte-range support, which is all an <audio> element needs.
export const DISCOVERY_NODE = (env.VITE_DISCOVERY_NODE ?? 'https://discoveryprovider.audius.co').replace(/\/$/, '');

/** Audius' attribution convention; required on every API call, no key needed. */
export const APP_NAME = env.VITE_APP_NAME ?? 'fangorn-demo';

/** Which wallet is the platform. The other publisher is the sovereign artist. */
export const PLATFORM_OWNER = (
  env.VITE_PLATFORM_OWNER ?? '0x1111111111111111111111111111111111111111'
).toLowerCase();
