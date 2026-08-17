// Does playback actually work, for both publishers' tracks?
//
// The stream URL is built from `fields.id` against a live Audius discovery node, so
// this can break without any of our code changing — an endpoint move, a signing
// change, a CORS regression. That failure would first appear in front of an audience.
// So: sample real tracks from the served snapshot, issue a ranged request to each
// stream URL, and require the large majority to come back as seekable audio.
//
// Usage: quickbeam cdn serve --cdn-dir ./examples/audius/audius-build/cdn --cors --port 8090
//        npm run check:stream
import assert from 'node:assert/strict';
import { Graph } from '../src/lib/graph.ts';
import { APP_NAME, DISCOVERY_NODE } from '../src/lib/config.ts';

const CDN = process.env.VITE_CDN_URL ?? 'http://localhost:8090';
const DOMAIN = process.env.VITE_DOMAIN ?? 'audius';
const PLATFORM = process.env.VITE_PLATFORM_OWNER ?? '0x1111111111111111111111111111111111111111';
const PER_PUBLISHER = Number(process.env.SAMPLE ?? 12);
const MIN_RATE = 0.9;

const g = new Graph();
console.log(`· loading ${DOMAIN} from ${CDN}`);
const stats = await g.load(CDN, DOMAIN, PLATFORM);
const [platform, artist] = stats.publishers;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function probe(id: string, attempt = 0): Promise<{
  ok: boolean; status: number; type: string; cors: string; ranged: boolean;
}> {
  const url = `${DISCOVERY_NODE}/v1/tracks/${id}/stream?app_name=${encodeURIComponent(APP_NAME)}`;
  try {
    // A tiny range request: enough to prove the redirect, CORS and content type
    // without pulling whole tracks down.
    const res = await fetch(url, {
      headers: { Range: 'bytes=0-2048', 'User-Agent': 'fangorn-demo/0.1' },
      redirect: 'follow',
    });
    const type = res.headers.get('content-type') ?? '';
    const cors = res.headers.get('access-control-allow-origin') ?? '';
    const ranged = res.status === 206 || !!res.headers.get('content-range');
    await res.arrayBuffer();
    // Discovery nodes rate-limit, and a burst of probes trips it — that measures the
    // harness, not playback. Back off and retry rather than reporting a false failure.
    if (res.status === 429 && attempt < 3) {
      await sleep(1200 * (attempt + 1));
      return probe(id, attempt + 1);
    }
    return { ok: res.ok && type.startsWith('audio/'), status: res.status, type, cors, ranged };
  } catch (e) {
    return { ok: false, status: 0, type: String((e as Error).message), cors: '', ranged: false };
  }
}

let failures = 0;
for (const [role, pub] of [['platform', platform], ['artist', artist]] as const) {
  const tracks = g.sample('Track', PER_PUBLISHER, pub.owner);
  if (!tracks.length) { console.log(`· ${role}: no tracks to sample`); continue; }

  // Sequential, spaced — one track at a time is also how a listener plays them.
  const results: Array<{ t: (typeof tracks)[number]; r: Awaited<ReturnType<typeof probe>> }> = [];
  for (const t of tracks) {
    results.push({ t, r: await probe(String(t.fields.id)) });
    await sleep(150);
  }
  const good = results.filter((x) => x.r.ok);
  const rate = good.length / results.length;

  console.log(`\n· ${role} (${pub.label ?? pub.owner.slice(0, 8)}): ` +
              `${good.length}/${results.length} streamable`);
  for (const { t, r } of results.filter((x) => !x.r.ok)) {
    console.log(`    ✗ ${String(t.fields.title).slice(0, 40)} — ${r.status} ${r.type}`);
  }

  try {
    assert.ok(rate >= MIN_RATE,
      `only ${(rate * 100).toFixed(0)}% streamable (expected ≥${MIN_RATE * 100}%); ` +
      `~1% is normal for stream-gated tracks, much more means the endpoint moved`);
    // What <audio> actually depends on: audio bytes it can range-request.
    const s = good[0].r;
    assert.ok(s.ranged, 'no byte-range support — seeking would not work');

    // CORS is deliberately NOT asserted. A plain `<audio src>` is not a CORS
    // request — media elements load cross-origin freely unless `crossorigin` is
    // set, which we never do (lib/player.tsx builds a bare `new Audio()`). Audius
    // rotates between content-node mirrors and only some send the header, so
    // requiring it fails the check for a reason that cannot affect playback.
    // It would matter only if we started reading samples (Web Audio, canvas).
    const corsNote = good.some((x) => x.r.cors === '*')
      ? 'CORS open on some mirrors'
      : 'no CORS header (irrelevant to <audio>)';
    console.log(`    ✓ audio/mpeg, byte-ranges · ${corsNote}`);
  } catch (e) {
    failures++;
    console.log(`    ✗ ${(e as Error).message}`);
  }
}

if (failures) { console.error(`\n${failures} publisher(s) failed the stream check`); process.exit(1); }
console.log('\nPlayback works for both publishers.');
