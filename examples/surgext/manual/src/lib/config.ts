// Semantic-CDN source. Default: same-origin /cdn (the baked snapshot staged into
// public/cdn by scripts/stage-cdn.mjs). Point VITE_CDN_URL at `/cdn-live` (the dev
// proxy to `quickbeam cdn serve`) to develop without staging.
const env = ((import.meta as { env?: Record<string, string | undefined> }).env) ?? {};

export const CDN_URL = (env.VITE_CDN_URL ?? '/cdn').replace(/\/$/, '');
export const CDN_DOMAIN = env.VITE_DOMAIN ?? 'surgext';

// IPFS gateway used to resolve a figure by its on-chain CID when the private, same-origin
// image bundle isn't present (i.e. a deploy that reads the graph from Fangorn rather than a
// baked CDN snapshot). The bundle is preferred, so this is a fallback only.
export const IPFS_GATEWAY = (env.VITE_IPFS_GATEWAY ?? 'https://ipfs.io').replace(/\/$/, '');
