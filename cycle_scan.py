import gzip, json, glob, collections

records = []
for path in sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz')):
    with gzip.open(path, 'rt') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

transfers = [r for r in records if 'txHash' in r['fields']]
assets_recs = [r for r in records if 'txHash' not in r['fields']]
print("asset-like records", len(assets_recs))
symbols = sorted(set(r['fields'].get('symbol') for r in assets_recs if r['fields'].get('symbol')))
print(len(symbols), symbols)

RING = {
    '0xc94135b63772b91d79d0a2daab2a8801f32359bd',
    '0x2f4579ca81717d3d61bf8b6f06571877bbe54a07',
    '0x624c6dbb5d1aae291c788ef116e69a59552b17c4',
    '0x33b0095333e64bf375952ef197b6fdc3437dc014',
}

per_symbol = collections.defaultdict(lambda: {'n': 0, 'ring': 0, 'wallets': set()})
for r in transfers:
    f = r['fields']
    sym = f.get('symbol')
    if not sym:
        continue
    d = per_symbol[sym]
    d['n'] += 1
    fa = (f.get('fromAddr') or '').lower()
    ta = (f.get('toAddr') or '').lower()
    if fa:
        d['wallets'].add(fa)
    if ta:
        d['wallets'].add(ta)
    if fa in RING or ta in RING:
        d['ring'] += 1

for sym in sorted(per_symbol, key=lambda s: -per_symbol[s]['ring'] / per_symbol[s]['n']):
    d = per_symbol[sym]
    pct = 100 * d['ring'] / d['n']
    print(f"{sym:8s} n={d['n']:5d} ring_pct={pct:5.1f} distinct_wallets={len(d['wallets'])}")
