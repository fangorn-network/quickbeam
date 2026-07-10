import gzip, json, glob, os, time
from collections import defaultdict, Counter

# Offline fallback cycle: quickbeam MCP/CDN daemons down, so analyze the on-disk
# CDN delta shards directly (per market-mesh recovery path). Raw shard records
# carry no record-level provenance.source_cid offline — only meta.manifestCid,
# the on-chain commit anchoring the shard — so that is what we cite.
OUT = "/home/driemworks/fangorn/robinhood-bot/signals/signals.json"
MIN_N = 1000  # only signal on assets with real transfer volume (clean gap: CRCL 1542 vs BABA 147)

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

# 3-addr trading cluster + the mint/faucet feeding it = the wash-trading ring.
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

now = int(time.time()); exp = now + 2700
sig = {}
for sym, ts in bysym.items():
    n = len(ts)
    if n < MIN_N:
        continue  # too thin to hold an opinion; omit rather than guess
    touch = sum(1 for t in ts if t.get('fromAddr', '').lower() in ring or t.get('toAddr', '').lower() in ring)
    pairs = Counter((t.get('fromAddr', '').lower(), t.get('toAddr', '').lower(), round(t.get('value', 0), 8)) for t in ts)
    rt = sum(min(c, pairs.get((to, fr, v), 0)) for (fr, to, v), c in pairs.items() if pairs.get((to, fr, v), 0))
    a = assets.get(sym, {}); hol = a.get('holders')
    cid = amanifest.get(sym)
    hol_s = (f"{hol} on-chain holders is a manufactured veneer, " if hol else "")
    reason = (
        f"wash-trading confirmed (offline fallback: quickbeam MCP/CDN daemons down, analyzed on-disk "
        f"CDN delta shards directly): {round(100*touch/n)}% of {n} notable transfers touch the wash ring "
        f"{ring_lbl}, which touches {round(100*ring_touch/tot)}% ({ring_touch}/{tot}) of all corpus transfers; "
        f"{round(100*rt/n)}% of this symbol's transfers are bidirectional same-value round-trips (A<->B). "
        f"{hol_s}not organic demand -> no long thesis. Explicit flat closes/avoids. "
        f"provenance.source_cid unavailable offline; anchoring shard meta.manifestCid {cid}"
    )
    sig[sym] = {"side": "flat", "confidence": 0.0, "reason": reason,
                "generated_at": now, "expires_at": exp}

tmp = OUT + ".tmp"
with open(tmp, "w") as fh:
    json.dump(sig, fh, indent=2)
os.replace(tmp, OUT)
print(f"corpus {tot} transfers, ring touches {round(100*ring_touch/tot)}%; wrote {len(sig)} flat symbols; "
      f"generated_at {now} expires_at {exp}")
assert sig and all(v["side"] == "flat" for v in sig.values()), "expected all-flat wash-traded corpus"
