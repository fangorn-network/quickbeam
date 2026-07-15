// Playground service — a thin HTTP wrapper over @fangorn-network/sdk that publishes
// into the REAL embedding pipeline (quickbeam watch → Qdrant).
//
// The watcher only ingests *bundle* manifests (it edge-walks to build documents),
// so a flat "schema + fields" from the UI is shaped here into a single-node bundle:
// we register the node schema, register a one-type bundle schema with a trivial
// self-edge, and publishBundle each record as a node with a self-edge. The watcher
// then embeds it with the default fold profile (document == the node's own fields).
// The browser never sees the key or the bundle plumbing.
//
// Env (same as the fangorn CLI): DELEGATOR_ETH_PRIVATE_KEY, PINATA_JWT,
// PINATA_GATEWAY, CHAIN_NAME (default arbitrumSepolia). PORT default 8791.

import { createServer } from 'node:http';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { Fangorn, FangornConfig, ObjectStore } from '@fangorn-network/sdk';
import { privateKeyToAccount } from 'viem/accounts';

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATE_PATH = join(__dirname, '.state.json');
const PORT = Number(process.env.PORT ?? 8791);
// Isolated by default so the playground never mixes with a shared `fangorn`
// collection's data (which would leak old, unrelated points into the CDN bake).
const COLLECTION = process.env.QDRANT_COLLECTION ?? 'playground';
// Stamped onto every commit so the watcher indexes at the dim the browser query
// embedder uses (nomic → matryoshka-256). Keep in sync with src/lib/embed.ts.
const EMBED_MODEL = process.env.EMBED_MODEL ?? 'nomic-ai/nomic-embed-text-v1.5';
const EMBED_DIM = Number(process.env.EMBED_DIM ?? 256);

// ---- config / sdk ---------------------------------------------------------

const PRIVATE_KEY = process.env.DELEGATOR_ETH_PRIVATE_KEY ?? '';
// Tolerate two common env quirks so the same .env that drives the Python pipeline
// works here: a stray leading '.' on the JWT, and a trailing '/ipfs' on the
// gateway. The TS SDK always appends '/ipfs/<cid>', so a gateway ending in '/ipfs'
// would double it (…/ipfs/ipfs/<cid> → Bad Request).
const PINATA_JWT = (process.env.PINATA_JWT ?? '').replace(/^\./, '');
const PINATA_GATEWAY = (process.env.PINATA_GATEWAY ?? '').replace(/\/ipfs\/?$/, '');
const CHAIN_NAME = process.env.CHAIN_NAME ?? 'arbitrumSepolia';
const CONFIGURED = Boolean(PRIVATE_KEY && PINATA_JWT && PINATA_GATEWAY);
const config = CHAIN_NAME === 'baseSepolia' ? FangornConfig.BaseSepolia : FangornConfig.ArbitrumSepolia;

let fangorn = null;
let OWNER = null;
if (CONFIGURED) {
  fangorn = Fangorn.create({
    privateKey: PRIVATE_KEY,
    storage: { pinata: { jwt: PINATA_JWT, gateway: PINATA_GATEWAY } },
    config,
    domain: 'localhost',
  });
  OWNER = privateKeyToAccount(PRIVATE_KEY).address;
}

function requireConfigured() {
  if (!CONFIGURED) {
    const err = new Error('Server not configured — set DELEGATOR_ETH_PRIVATE_KEY, PINATA_JWT, PINATA_GATEWAY and restart.');
    err.status = 503;
    throw err;
  }
}

// ---- state ----------------------------------------------------------------

function loadState() {
  const empty = { schemas: {}, datasets: {}, published: [] };
  if (!existsSync(STATE_PATH)) return empty;
  try {
    const s = JSON.parse(readFileSync(STATE_PATH, 'utf8'));
    return { schemas: s.schemas ?? {}, datasets: s.datasets ?? {}, published: s.published ?? [] };
  } catch {
    return empty;
  }
}
function saveState() {
  writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}
let state = loadState();

// A node schema name → its bundle name + dataset. Deterministic so re-registering
// the same schema is idempotent and the watch command is stable.
const bundleNameFor = (name) => (/\.v\d+$/.test(name) ? name.replace(/\.v(\d+)$/, '.bundle.v$1') : `${name}.bundle`);

// After registering a node schema, its blob is freshly pinned — reading it back
// through the gateway (which resolveBundle does) can 404 for a few seconds. Poll
// schema.get until it resolves so the bundle registration doesn't race the pin.
async function waitForSchema(name, tries = 20, delayMs = 3000) {
  for (let i = 0; i < tries; i++) {
    try {
      const s = await fangorn.schema.get(name);
      if (s?.schemaId && s.definition) return s;
    } catch { /* not visible yet */ }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  throw new Error(`schema "${name}" did not become resolvable on-chain/IPFS in time`);
}

// Reconstruct a dataset's HEAD + full record set from its on-chain tip (the
// git-native clone property). Called when the server has no local history for a
// dataset but the chain already has a tip — so appends build on the real tip and
// commit the real current snapshot rather than clobbering it.
async function hydrateFromTip(s) {
  const tip = await fangorn.publisher.resolveTip(OWNER, s.bundleSchemaId, s.dataset);
  if (!tip) return { schemaName: s.name, head: undefined, records: [] };
  const objects = new ObjectStore(fangorn.getStorage());
  let manifest;
  try {
    const commit = await objects.getCommit(tip); // git-native tip wraps the tree
    manifest = await objects.getTree(commit);
  } catch {
    manifest = await fangorn.publisher.getBundleManifestByCid(tip); // legacy raw tip
  }
  const { nodesById } = await fangorn.publisher.readBundle(manifest);
  const records = [...nodesById.values()].map((n) => ({ id: n.id, fields: n.fields }));
  return { schemaName: s.name, head: tip, records };
}

function watchCommand(s) {
  // NB: no --dataset filter. Every publish after the first is a git-native tip
  // UPDATE (ManifestUpdated), and those events carry no dataset name — so a
  // --dataset filter drops them and the watcher never ingests anything past the
  // initial publish. The bundle schema id already scopes the watch to this dataset.
  //
  // --collection/--checkpoint-file isolate this dataset's index + cursor. The
  // --cdn-* flags make the watcher bake an initial snapshot on start (if absent)
  // and append each embed cycle as a delta shard — so `cdn serve` needs no manual
  // bake. Serve ./cdn-playground on :8090 (the app proxies /cdn there).
  return (
    `quickbeam watch --bundle ${s.bundleName}=${s.bundleSchemaId} ` +
    `--root-type ${s.type} ` +
    `--collection ${COLLECTION} ` +
    `--checkpoint-file ./db/${COLLECTION}_checkpoint.json ` +
    `--poll-interval 30 ` +
    `--cdn-dir ./cdn-playground --cdn-domain ${s.dataset.toLowerCase()} ` +
    `$BUILD_AUTH`
  );
}

// ---- handlers -------------------------------------------------------------

const routes = {
  'GET /health': async () => ({
    ok: true,
    configured: CONFIGURED,
    owner: OWNER,
    chain: config.chainName,
    collection: COLLECTION,
  }),

  // Everything we've registered locally (drives the "kinds" dropdown/chips).
  'GET /schemas': async () => ({ schemas: Object.values(state.schemas) }),

  // Fetch a schema straight from the on-chain registry ("summon"). Works for any
  // resolver schema, whether or not this server registered it.
  'GET /schema/summon': async (_b, q) => {
    requireConfigured();
    const nameOrId = q.get('name');
    if (!nameOrId) {
      const e = new Error('pass ?name=<schema name or id>');
      e.status = 400;
      throw e;
    }
    const schema = await fangorn.schema.get(nameOrId);
    if (!schema) {
      const e = new Error(`no schema "${nameOrId}" on-chain`);
      e.status = 404;
      throw e;
    }
    // Reduce a resolver definition back to the { field: scalar } shape the UI edits.
    const fields = {};
    for (const [k, def] of Object.entries(schema.definition ?? {})) {
      fields[k] = typeof def?.['@type'] === 'string' ? def['@type'] : 'string';
    }
    return { name: schema.name, schemaId: schema.schemaId, kind: schema.kind, fields, known: state.schemas[schema.name] ?? null };
  },

  // Register a flat schema: a resolver node schema + a one-type bundle wrapping it.
  // body: { name, type, titleField, fields: { field: scalar } }
  'POST /schema/register': async (body) => {
    requireConfigured();
    const { name, type, titleField } = body;
    const fields = body.fields ?? {};
    if (!name || !type || Object.keys(fields).length === 0) {
      const e = new Error('register needs { name, type, fields: { field: scalar } }');
      e.status = 400;
      throw e;
    }

    const definition = {};
    for (const [k, t] of Object.entries(fields)) definition[k] = { '@type': t };

    // 1. node schema (idempotent — register returns the existing id if it exists)
    const node = await fangorn.schema.register({ name, definition });
    // Wait for the node schema to be readable before the bundle references it.
    await waitForSchema(name);

    // 2. bundle schema referencing the node type, with a self-edge so the watcher's
    //    edge-walk join has an edge to traverse (it skips edge-less manifests).
    const bundleName = bundleNameFor(name);
    const bundle = await fangorn.schema.register({
      kind: 'bundle',
      name: bundleName,
      bundle: {
        nodes: { [type]: name },
        edges: [{ rel: 'self', from: type, to: type, min: 0, max: 1 }],
      },
    });

    const entry = {
      name,
      label: body.label ?? type,
      type,
      titleField: titleField ?? Object.keys(fields)[0],
      fields,
      nodeSchemaId: node.schemaId,
      bundleName,
      bundleSchemaId: bundle.schemaId,
      dataset: type,
    };
    state.schemas[name] = entry;
    saveState();
    return { ...entry, watchCommand: watchCommand(entry) };
  },

  // Publish records of a known schema into the real pipeline. Git-native path:
  // commit a bundle (nodes + trivial self-edges) on the dataset's local HEAD, then
  // push the on-chain tip. The commit carries an embed contract so the watcher
  // indexes it at the same model/dim the browser query embedder uses (256-d).
  // body: { schemaName, records: [{ fields }] }
  'POST /publish': async (body) => {
    requireConfigured();
    const { schemaName, records } = body;
    const s = state.schemas[schemaName];
    if (!s) {
      const e = new Error(`unknown schema "${schemaName}" — register it first`);
      e.status = 400;
      throw e;
    }
    if (!Array.isArray(records) || records.length === 0) {
      const e = new Error('publish needs { records: [{ fields }] }');
      e.status = 400;
      throw e;
    }

    // A commit is a full snapshot of the dataset (like a git working tree), not a
    // delta — the watcher tombstones anything present in the previous tip but
    // absent from the new one, so committing only the new records would delete the
    // rest. Accumulate the full set and commit all of it; structural sharing means
    // unchanged chunks aren't re-uploaded.
    const ds = state.datasets[s.dataset] ?? (await hydrateFromTip(s));
    const added = records.map((r, i) => ({
      id: r.id || `${s.type.toLowerCase()}-${Date.now().toString(36)}-${i}`,
      fields: r.fields ?? {},
    }));
    ds.records = [...ds.records, ...added];

    const nodes = ds.records.map((r) => ({ id: r.id, type: s.type, fields: r.fields }));
    // The watcher's bundle join edge-walks to build documents and skips edge-less
    // manifests, so give each node a self-edge (the default fold profile then emits
    // just the node's own fields as its document).
    const edges = nodes.map((n) => ({ rel: 'self', from: n.id, to: n.id }));

    const parents = ds.head ? [ds.head] : [];
    const commit = await fangorn.publisher.commitBundle({
      bundleName: s.bundleName,
      nodes,
      edges,
      datasetName: s.dataset,
      parents,
      message: body.message ?? `+${added.length} ${s.type} (${nodes.length} total)`,
      embed: { model: EMBED_MODEL, dim: EMBED_DIM, distance: 'Cosine' },
    });
    const { txHash, onChainTip } = await fangorn.publisher.push({
      commitCid: commit.commitCid,
      root: commit.root,
      schemaId: s.bundleSchemaId,
      datasetName: s.dataset,
      expectedParent: parents[0],
    });

    ds.head = commit.commitCid;
    state.datasets[s.dataset] = ds;

    const entry = {
      schemaName,
      type: s.type,
      dataset: s.dataset,
      commitCid: commit.commitCid,
      tree: commit.manifestCid,
      parent: parents[0] ?? null,
      tip: onChainTip,
      txHash,
      entryCount: commit.entryCount, // total records in this snapshot
      uploadedCount: commit.uploadedCount,
      reusedCount: commit.reusedCount,
      count: added.length, // records added this publish
      records: added, // just the newly-added records
      at: new Date().toISOString(),
    };
    state.published.unshift(entry);
    saveState();
    return entry;
  },

  // Everything we've published: the current full record set per dataset (the big
  // JSON viewer) plus the commit log (newest first).
  'GET /published': async () => ({
    datasets: Object.entries(state.datasets).map(([dataset, ds]) => {
      const s = state.schemas[ds.schemaName];
      return {
        dataset,
        schemaName: ds.schemaName,
        type: s?.type ?? dataset,
        head: ds.head ?? null,
        count: ds.records.length,
        records: ds.records,
      };
    }),
    published: state.published,
  }),
};

// ---- http plumbing --------------------------------------------------------

function send(res, status, obj) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  });
  res.end(JSON.stringify(obj));
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        resolve({});
      }
    });
  });
}

const server = createServer(async (req, res) => {
  if (req.method === 'OPTIONS') return send(res, 204, {});
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const handler = routes[`${req.method} ${url.pathname}`];
  if (!handler) return send(res, 404, { error: `no route ${req.method} ${url.pathname}` });
  try {
    const body = req.method === 'POST' ? await readBody(req) : {};
    send(res, 200, await handler(body, url.searchParams));
  } catch (err) {
    const status = err?.status ?? 500;
    console.error(`${req.method} ${url.pathname} failed:`, err?.message ?? err);
    send(res, status, { error: err?.message ?? String(err) });
  }
});

server.listen(PORT, () => {
  console.log(`playground service on http://localhost:${PORT}`);
  console.log(CONFIGURED ? `  configured · owner ${OWNER} · chain ${config.chainName} · collection ${COLLECTION}` : '  NOT configured — set env vars (see server/README.md); search still works if Qdrant is up');
});
