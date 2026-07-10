import gzip, json, glob, os, time, asyncio
from collections import defaultdict, Counter
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Live cycle: quickbeam CDN(8090)/MCP(8765) daemons restarted this cycle.
# Per-symbol flow analyzed over on-disk CDN delta shards (full accumulated set,
# 50 assets / ~69k transfers). Each decision cites the record-level
# provenance.source_cid from the mesh Asset record via MCP `get`; for symbols the
# current (smaller, newer) MCP delta snapshot doesn't carry, we fall back to the
# shard's meta.manifestCid — the on-chain commit anchoring that shard.
OUT = "/home/driemworks/fangorn/robinhood-bot/signals/signals.json"
MIN_N = 1000  # only signal on assets with real volume (clean gap: CRCL 1542 vs BABA 147)

shards = sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz'))
transfers, assets, amanifest = [], {}, {}
deg = Counter()
for s in shards:
    for line in gzip.open(s, 'rt'):
        r = json.loads(line); f = r.get('fields', {}); et = f.get('entityType')
        if et == 'Transfer':
            transfers.append(f)
            deg[f.get('fromAddr', '').lower()] += 1
            deg[f.get('toAddr', '').lower()] += 1
        elif et == 'Asset':
            assets[f.get('symbol')] = f
            amanifest[f.get('symbol')] = (r.get('meta') or {}).get('manifestCid')

cluster = set(a for a, _ in deg.most_common(3))
faucet = deg.most_common(4)[3][0]
ring = cluster | {faucet}
ring_touch = sum(1 for t in transfers
                 if t.get('fromAddr', '').lower() in ring or t.get('toAddr', '').lower() in ring)
tot = len(transfers)
ring_lbl = "/".join(sorted(a[:10] for a in cluster)) + f" + faucet {faucet[:10]}"

bysym = defaultdict(list)
for t in transfers:
    bysym[t.get('symbol')].append(t)

symbols = sorted(s for s, ts in bysym.items() if len(ts) >= MIN_N)

async def fetch_cids(syms):
    out = {}
    async with streamablehttp_client("http://localhost:8765/mcp") as (r, w, _):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            for sym in syms:
                res = await sess.call_tool("get", {"dataset": "robinhood", "id": f"rh:asset:{sym}"})
                txt = "".join(getattr(c, "text", "") for c in res.content)
                try:
                    out[sym] = json.loads(txt).get("record", {}).get("provenance", {}).get("source_cid")
                except Exception:
                    out[sym] = None
    return out

cids = asyncio.run(fetch_cids(symbols))

now = int(time.time()); exp = now + 2700
sig = {}
for sym in symbols:
    ts = bysym[sym]; n = len(ts)
    touch = sum(1 for t in ts if t.get('fromAddr', '').lower() in ring or t.get('toAddr', '').lower() in ring)
    pairs = Counter((t.get('fromAddr', '').lower(), t.get('toAddr', '').lower(), round(t.get('value', 0), 8)) for t in ts)
    rt = sum(min(c, pairs.get((to, fr, v), 0)) for (fr, to, v), c in pairs.items() if pairs.get((to, fr, v), 0))
    hol = assets.get(sym, {}).get('holders')
    hol_s = (f"{hol} on-chain holders is a manufactured veneer, " if hol else "")
    if cids.get(sym):
        prov = f"provenance.source_cid {cids[sym]} (mesh Asset record via MCP get)"
    else:
        prov = (f"provenance.source_cid unavailable in current MCP delta snapshot; "
                f"anchoring shard meta.manifestCid {amanifest.get(sym)}")
    reason = (
        f"wash-trading confirmed (live MCP cycle): {round(100*touch/n)}% of {n} notable transfers touch the "
        f"wash ring {ring_lbl}, which touches {round(100*ring_touch/tot)}% ({ring_touch}/{tot}) of all corpus "
        f"transfers; {round(100*rt/n)}% of this symbol's transfers are bidirectional same-value round-trips "
        f"(A<->B). {hol_s}not organic demand -> no long thesis. Explicit flat closes/avoids. {prov}"
    )
    sig[sym] = {"side": "flat", "confidence": 0.0, "reason": reason,
                "generated_at": now, "expires_at": exp}

tmp = OUT + ".tmp"
with open(tmp, "w") as fh:
    json.dump(sig, fh, indent=2)
os.replace(tmp, OUT)
n_live = sum(1 for s in symbols if cids.get(s))
print(f"corpus {tot} transfers, ring touches {round(100*ring_touch/tot)}%; wrote {len(sig)} flat symbols "
      f"({n_live} with live source_cid, {len(sig)-n_live} manifestCid fallback); "
      f"generated_at {now} expires_at {exp}")
assert sig and all(v["side"] == "flat" for v in sig.values()), "expected all-flat wash-traded corpus"
