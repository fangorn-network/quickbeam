import json, gzip, glob
from collections import Counter, defaultdict
base = '/home/driemworks/fangorn/embeddings/cdn/robinhood'
assets = {}
xfers = []
wallet_touch = Counter()
wallet_by_asset = defaultdict(Counter)
sample_cid = None
for sh in sorted(glob.glob(base + '/shard-*.ndjson.gz')):
    with gzip.open(sh, 'rt') as f:
        for line in f:
            r = json.loads(line)
            tid = r.get('track_id', '')
            fl = r.get('fields', {})
            meta = r.get('meta', {})
            cid = meta.get('source_cid') or (meta.get('provenance', {}) or {}).get('source_cid')
            if tid.startswith('rh:asset:'):
                assets[fl.get('symbol')] = {
                    'holders': fl.get('holders'), 'price': fl.get('price'),
                    'sector': fl.get('sector'), 'mcap': fl.get('marketCap'),
                    'recentVolume': fl.get('recentVolume'),
                    'recentTransfers': fl.get('recentTransfers'), 'cid': cid}
            elif tid.startswith('rh:xfer:'):
                sym = fl.get('symbol')
                fr = (fl.get('fromAddr') or '').lower()
                to = (fl.get('toAddr') or '').lower()
                xfers.append((sym, fr, to, fl.get('value'), fl.get('usdValue')))
                for w in (fr, to):
                    if w:
                        wallet_touch[w] += 1
                        wallet_by_asset[sym][w] += 1
                if sample_cid is None:
                    sample_cid = cid

print("num assets:", len(assets), " num xfers:", len(xfers))
print("sample xfer cid:", sample_cid)
anyasset = next(iter(assets.values()))
print("sample asset cid:", anyasset.get('cid'))

zero_prefix = '0x0000000000000000000000000000000000000000'
mint = [w for w in wallet_touch if w == zero_prefix]
print("mint zero addr touches:", [(m, wallet_touch[m]) for m in mint])

print("\nTOP WALLETS (corpus):")
for w, c in wallet_touch.most_common(8):
    print("  ", w, c)

ring = set(w for w, _ in wallet_touch.most_common(4))
tot_all = sum(wallet_touch.values())
ring_all = sum(wallet_touch[w] for w in ring)
print("\nCorpus ring share (top4 / all touches): %d/%d = %.3f" % (ring_all, tot_all, ring_all / tot_all))

per_sym_xfers = Counter(x[0] for x in xfers)
print("\nPER-SYMBOL  sym holders price sector nXfers ringShare topWalletShare")
rows = []
for sym, a in assets.items():
    n = per_sym_xfers.get(sym, 0)
    wc = wallet_by_asset[sym]
    tot = sum(wc.values())
    ringtouch = sum(c for w, c in wc.items() if w in ring)
    ringshare = ringtouch / tot if tot else 0
    topshare = (wc.most_common(1)[0][1] / tot) if tot else 0
    rows.append((sym, a.get('holders'), a.get('price'), a.get('sector'), n, round(ringshare, 3), round(topshare, 3)))
for r in sorted(rows, key=lambda x: -(x[4] or 0)):
    print("  %-6s h=%-5s $%-8s %-12s n=%-5s ring=%-5s top=%s" % r)
