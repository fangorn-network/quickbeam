// Vite injects `import.meta.env` at BUILD time, so under node — which is how
// scripts/check-*.ts drive this same code — it does not exist. Without the process.env
// fallback a node check runs with every option unset and quietly exercises a different
// configuration than the browser does: check:remote saw API_URL='' and concluded the
// bucket path was broken when it was simply never switched on.
const viteEnv = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};
const procEnv = (typeof process !== 'undefined' && process.env
  ? process.env : {}) as Record<string, string | undefined>;
const env = { ...procEnv, ...viteEnv };

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

// ── the hosted half ─────────────────────────────────────────────────────────
// The private-retrieval API (`quickbeam serve`). Empty disables it entirely and the
// app behaves exactly like audius-demo: it searches only what it downloaded, and
// nothing about a query ever leaves the tab. Set it and search reaches the whole
// catalogue by disclosing one bucket id per uncached region — see src/lib/store.ts,
// the single file in that path that touches the network.
export const API_URL = (env.VITE_API_URL ?? '').replace(/\/$/, '');

// Cells probed per query. Recall and bytes both scale with it; disclosure does not
// beyond the buckets those cells fall into. Measured: nprobe=1 gives ~88% of the
// achievable similarity, nprobe=4 ~93%. See audius-status.txt.
export const NPROBE = Number(env.VITE_NPROBE ?? 1);

// The domain the CODEBOOK is served from. The bootstrap graph (VITE_DOMAIN) is a
// slice; the codebook indexes the FULL corpus behind the API, so the two differ.
export const INDEX_DOMAIN = env.VITE_INDEX_DOMAIN ?? '';
