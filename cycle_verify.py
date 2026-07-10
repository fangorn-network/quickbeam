import gzip, json, glob, collections

files = sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz'))
transfers = []
for f in files:
    with gzip.open(f, 'rt') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            flds = rec.get('fields', {})
            if flds.get('entityType') == 'Transfer':
                transfers.append(flds)

low_vol_syms = {'BABA','ASTS','SOFI','MSTR','RBLX','RKLB','EWY','RDW','FLNC','NBIS','IREN',
                'TTWO','AMAT','ASML','GME','QCOM','DDOG','RDDT','TSM','GLW','APLD','LITE',
                'CLSK','RGTI','IONQ','COST'}

wc = collections.Counter()
mint_touches = 0
for t in transfers:
    if t.get('symbol') in low_vol_syms:
        f_ = t.get('fromAddr', '').lower()
        to = t.get('toAddr', '').lower()
        wc[f_] += 1
        wc[to] += 1
        if f_.startswith('0x000') or to.startswith('0x000'):
            mint_touches += 1

print('top wallets in low-vol symbol set:')
for w, c in wc.most_common(15):
    print(f'  {w}: {c}')
print('mint (0x000...) touches:', mint_touches)

sym_by_wallet = collections.defaultdict(set)
for t in transfers:
    if t.get('symbol') in low_vol_syms:
        f_ = t.get('fromAddr', '').lower()
        to = t.get('toAddr', '').lower()
        sym_by_wallet[f_].add(t.get('symbol'))
        sym_by_wallet[to].add(t.get('symbol'))

top8 = [w for w, c in wc.most_common(8)]
for w in top8:
    print(w, len(sym_by_wallet[w]), sorted(sym_by_wallet[w]))
