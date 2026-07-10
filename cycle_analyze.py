import gzip, json, glob
from collections import defaultdict, Counter

shards = sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz'))
transfers = []
assets = {}
manifest_cids = set()
deg = Counter()

for s in shards:
    for line in gzip.open(s, 'rt'):
        r = json.loads(line)
        f = r.get('fields', {})
        et = f.get('entityType')
        mc = (r.get('meta') or {}).get('manifestCid')
        if mc:
            manifest_cids.add(mc)
        if et == 'Transfer':
            transfers.append(f)
            deg[f.get('fromAddr', '').lower()] += 1
            deg[f.get('toAddr', '').lower()] += 1
        elif et == 'Asset':
            assets[f.get('symbol')] = f

print('transfers', len(transfers), 'assets', len(assets))
top = deg.most_common(6)
print('top wallets:', [(a[:12], c) for a, c in top])
cluster = set(a for a, c in deg.most_common(3))
print('cluster:', [c[:12] for c in cluster])

bysym = defaultdict(list)
for t in transfers:
    bysym[t.get('symbol')].append(t)

rows = []
for sym, ts in bysym.items():
    n = len(ts)
    clu = sum(1 for t in ts if t.get('fromAddr', '').lower() in cluster or t.get('toAddr', '').lower() in cluster)
    pairs = Counter((t.get('fromAddr', '').lower(), t.get('toAddr', '').lower(), round(t.get('value', 0), 8)) for t in ts)
    matched = 0
    for (fr, to, v), c in pairs.items():
        rev = pairs.get((to, fr, v), 0)
        if rev > 0:
            matched += min(c, rev)
    a = assets.get(sym, {})
    rows.append((sym, n, round(100 * clu / n), round(100 * matched / n), a.get('holders'), a.get('price')))

rows.sort(key=lambda x: -x[1])
print('sym n clu% rt% holders price')
for r in rows:
    print(r)
print('manifest_cids:', sorted(manifest_cids))
print('live_assets:', sorted(assets.keys()))
