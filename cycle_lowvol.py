import gzip, json, glob, collections

records = []
for path in sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz')):
    with gzip.open(path, 'rt') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get('fields', {}).get('entityType') == 'Transfer':
                records.append(r)

NON_RING_SYMBOLS = {
    'RKLB','TSM','RBLX','FLNC','DDOG','MSTR','RGTI','EWY','RDW','SOFI',
    'BABA','APLD','ASML','NBIS','RDDT','IONQ','IREN','ASTS','GME','COST',
    'LITE','TTWO','GLW','QCOM','AMAT','CLSK',
}

by_symbol = collections.defaultdict(list)
wallet_symbols = collections.defaultdict(set)
for r in records:
    f = r['fields']
    sym = f.get('symbol')
    if sym in NON_RING_SYMBOLS:
        by_symbol[sym].append(f)
        for addr in (f.get('fromAddr'), f.get('toAddr')):
            if addr:
                wallet_symbols[addr.lower()].add(sym)

# check for wallets shared across multiple non-ring symbols (farming across assets)
shared = {w: syms for w, syms in wallet_symbols.items() if len(syms) > 1}
print(f"wallets touching >1 non-ring symbol: {len(shared)}")
for w, syms in list(shared.items())[:20]:
    print(f"  {w}: {sorted(syms)}")

print()
for sym in ['RKLB', 'BABA', 'ASTS', 'GME']:
    print(f"--- {sym} sample transfers ---")
    for f in by_symbol[sym][:6]:
        print(f"  {f.get('fromAddr')} -> {f.get('toAddr')}  value={f.get('value')} usd={f.get('usdValue')}")
    values = [f.get('value') for f in by_symbol[sym]]
    print(f"  distinct values: {sorted(set(values))[:10]} (n_transfers={len(values)})")
