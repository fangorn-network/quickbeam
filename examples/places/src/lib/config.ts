// Data-source selection.
//   mock   (default) — in-browser generated dataset, no backend.
//   qdrant           — REST client against a real Qdrant via the dev proxy.
//   shards           — download a CDN vector snapshot and search it in-browser.
const env = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};

export const DATA_SOURCE = env.VITE_DATA_SOURCE ?? 'mock';
export const IS_MOCK = DATA_SOURCE === 'mock';
export const IS_SHARDS = DATA_SOURCE === 'shards';
export const IS_QDRANT = DATA_SOURCE === 'qdrant';
export const USE_MOCK = IS_MOCK; // back-compat alias

// Semantic CDN (used when DATA_SOURCE=shards). Default to a local `quickbeam cdn
// serve`. VITE_DOMAIN empty → the first domain in the catalog.
export const CDN_URL = (env.VITE_CDN_URL ?? 'http://localhost:8090').replace(/\/$/, '');
export const CDN_DOMAIN = env.VITE_DOMAIN ?? '';

// Privy (Phase 3). Login is OPTIONAL — it gates actions (claim/tip), never
// discovery. Override the app id with VITE_PRIVY_APP_ID.
export const PRIVY_APP_ID = env.VITE_PRIVY_APP_ID ?? 'cmqad9j9b00i10cl7apzw6rq8';

// Claim/ownership UI (the ProfileOwnership strip: "claim this profile" CTA + the
// inline publish form). On by default; set VITE_CLAIMS=off to hide it entirely, e.g.
// for deployments where on-chain publishing isn't wired yet.
export const CLAIMS_ENABLED = (env.VITE_CLAIMS ?? 'on') !== 'off';
