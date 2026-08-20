// Stream a Quickbeam view's embeddings to a client and keep them current.
//
//   node javascript-example.js <viewId>          (WORKER overrides the registry URL)
//
// A view hands back two URLs (the registry worker's withUrls()): `streamUrl`, an SSE
// feed saying WHICH domain changed, and `cdnUrl`, the shard proxy. This wires them
// together: pull what exists, then hold the stream open and pull only the shards each
// commit adds. That is the point of the split — the events are notifications, so a
// reconnect costs nothing and the rows still arrive over the cacheable, content-hashed
// shard route with their manifest digests intact.
//
// What this assumes about the server, so a future reader can tell when it has gone stale:
//   • shards are gzip, served as application/gzip with NO Content-Encoding, so the
//     fetch layer does not decompress them for you (quickbeam/cdn.py, shard route);
//   • a shard filename embeds a hash of its bytes, so a name you have seen is bytes
//     you already hold;
//   • deletions ride manifest.tombstones, because shards themselves never change;
//   • stored vectors are L2-normalised (ingest/identity.py matryoshka), so a dot
//     product IS cosine similarity.

const WORKER = process.env.WORKER || 'https://quickbeam-registry.quickbeam.workers.dev';
const VIEW_ID = process.argv[2] || process.env.VIEW_ID;

const rows = new Map();     // track_id -> row. A later shard wins: an updated record
                            // re-ships under the same id rather than as a new one.
const loaded = new Set();   // shard files already applied

/**
 * Yield one row per line of a gzipped NDJSON response, as it arrives.
 *
 * Streaming rather than `await res.text()` is not a nicety here: a domain is routinely
 * hundreds of MB decompressed, and buffering it costs that much resident memory before
 * the first row is usable.
 */
async function* ndjson(res) {
  const reader = res.body
    .pipeThrough(new DecompressionStream('gzip'))
    .pipeThrough(new TextDecoderStream())
    .getReader();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    // A chunk boundary lands mid-line as often as not, so only whole lines are parsed
    // and the remainder carries into the next read.
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buf.trim()) yield JSON.parse(buf.trim());
}

/** Fetch one shard into `rows`. Returns how many rows it contributed. */
async function loadShard(cdnUrl, domain, file) {
  if (loaded.has(file)) return 0;
  // Claimed before the await so two overlapping syncs cannot both fetch it.
  loaded.add(file);
  const res = await fetch(`${cdnUrl}/domains/${domain}/shards/${file}`);
  if (!res.ok) {
    loaded.delete(file);
    throw new Error(`${file}: HTTP ${res.status}`);
  }
  let n = 0;
  for await (const row of ndjson(res)) {
    // {track_id, fields, embedding, owner, meta:{namespace, sourceCid}} — the same
    // shape /bundle/export emits, so anything reading one reads the other.
    if (!row.track_id || !row.embedding) continue;
    rows.set(row.track_id, row);
    n++;
  }
  return n;
}

/**
 * Bring one domain fully up to date: every shard the manifest lists, then its
 * tombstones. The manifest is the mutable pointer (revalidated, never cached hard)
 * that names the immutable shards.
 */
async function syncDomain(cdnUrl, domain) {
  const res = await fetch(`${cdnUrl}/domains/${domain}/manifest`);
  if (!res.ok) throw new Error(`manifest ${domain}: HTTP ${res.status}`);
  const manifest = await res.json();
  for (const shard of manifest.shards ?? []) await loadShard(cdnUrl, domain, shard.file);
  for (const id of manifest.tombstones ?? []) rows.delete(id);
  return manifest;
  // Note the one gap this leaves: /events only emits `change` when a commit produced a
  // new shard, so a re-bake that ONLY deletes records notifies nobody and the removed
  // rows survive here until the next full sync (a reconnect, or `added`). Call this
  // periodically if stale deletes matter more to you than the manifest fetch costs.
}

/**
 * Minimal Server-Sent Events reader over fetch, yielding {event, data}.
 *
 * A browser has EventSource for exactly this, but Node still hides it behind
 * --experimental-eventsource (checked on 22.14), and an example that needs a flag to
 * start is not an example. This is the same few lines, runs unchanged in both, and
 * reuses the reader pattern the shard path already uses. What it gives up is
 * EventSource's automatic reconnect — see follow().
 */
async function* sse(url) {
  const res = await fetch(url, { headers: { accept: 'text/event-stream' } });
  if (!res.ok) throw new Error(`stream: HTTP ${res.status}`);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    // One event per blank-line-separated block. The server's `: ping` heartbeats parse
    // to no data and drop out here, which is all they are for.
    let sep;
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      let event = 'message';
      let data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      if (data) yield { event, data };
    }
  }
}

// One promise chain per domain. Events can land while a sync is still running, and two
// interleaved syncs of the same domain would double-fetch and race on the tombstones.
const chains = new Map();
function serial(domain, fn) {
  const next = (chains.get(domain) ?? Promise.resolve())
    .then(fn)
    .catch((err) => console.error(`[${domain}]`, err.message));
  chains.set(domain, next);
  return next;
}

/** Hold the view open: initial pull, then a pull per change, forever. */
async function follow({ streamUrl, cdnUrl }) {
  for (;;) {
    try {
      for await (const { event, data } of sse(streamUrl)) {
        const payload = JSON.parse(data);
        const { domain } = payload;

        // `snapshot` fires once per domain the instant you connect — your starting
        // point, with no separate "what is there?" request. `added` is a namespace's
        // first bake: same handling, it simply was not there before.
        if (event === 'snapshot' || event === 'added') {
          serial(domain, async () => {
            const manifest = await syncDomain(cdnUrl, domain);
            console.log(`[${domain}] ${rows.size}/${manifest.count} rows, dim ${manifest.dim}`);
          });
        } else if (event === 'change') {
          // A commit landed. The event names the shards it produced, so this is the
          // cheap path — no manifest, no re-scan, just the new bytes.
          serial(domain, async () => {
            for (const file of payload.added) {
              console.log(`[${domain}] +${await loadShard(cdnUrl, domain, file)} rows from ${file}`);
            }
          });
        }
      }
    } catch (err) {
      console.warn(`stream error (${err.message})`);
    }
    // Reconnect, the one thing EventSource would have done for us. Safe to redo from
    // scratch: the snapshot that follows re-lists every shard and syncDomain skips the
    // ones already held, so nothing is refetched and nothing is missed.
    console.warn('stream ended — reconnecting in 2s');
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

/**
 * Nearest rows to a query vector. A plain dot product: every stored vector is
 * L2-normalised at ingest, so this is cosine similarity with no division.
 *
 * The query vector is yours to produce — this client carries no model. If you have no
 * encoder, `${searchUrl}?q=…` runs the whole query server-side instead.
 */
function search(queryVec, k = 10) {
  return [...rows.values()]
    .map((row) => ({
      row,
      score: row.embedding.reduce((sum, v, i) => sum + v * queryVec[i], 0),
    }))
    .sort((a, b) => b.score - a.score)
    .slice(0, k);
}

// ── Run it ──────────────────────────────────────────────────────────────────
// Deliberately no `import`/`export` and no top-level await anywhere in this file: with
// no package.json beside it Node reads a bare .js as CommonJS, and either would be a
// SyntaxError before the first line runs. Copy the functions into whatever module
// system you actually use.
async function main() {
  if (!VIEW_ID) {
    console.error('usage: node javascript-example.js <viewId>   (or set VIEW_ID)');
    process.exit(1);
  }
  const view = await fetch(`${WORKER}/views/${VIEW_ID}`).then((r) => r.json());
  if (!view.streamUrl) {
    console.error(`No view ${VIEW_ID}: ${view.error ?? 'unknown'}`);
    process.exit(1);
  }
  console.log(`following ${view.name} — ${view.sources.length} namespace(s)`);
  await follow(view);
}

main();
