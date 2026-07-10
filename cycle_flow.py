import csv, glob
from collections import defaultdict, Counter
path = sorted(glob.glob('/tmp/quickbeam-exports/robinhood-Transfer-*.csv'))[-1]
rows = list(csv.DictReader(open(path)))
deg = Counter()
for r in rows:
    deg[r['fromAddr'].lower()] += 1
    deg[r['toAddr'].lower()] += 1
cluster = set(a for a, _ in deg.most_common(3))
clu_sent = sum(1 for r in rows if r['fromAddr'].lower() in cluster)
bysym = defaultdict(list)
for r in rows:
    bysym[r['symbol']].append(r)
out = []
for sym, ts in bysym.items():
    n = len(ts)
    clu = sum(1 for t in ts if t['fromAddr'].lower() in cluster or t['toAddr'].lower() in cluster)
    pairs = Counter((t['fromAddr'].lower(), t['toAddr'].lower(), t['value']) for t in ts)
    matched = 0
    for (fr, to, v), c in pairs.items():
        rev = pairs.get((to, fr, v), 0)
        if rev > 0:
            matched += min(c, rev)
    out.append((sym, n, clu, round(100 * clu / n), round(100 * matched / n)))
out.sort(key=lambda x: -x[1])
print('total rows', len(rows), 'cluster-sent', clu_sent, round(100*clu_sent/len(rows)), '%')
print('cluster', [a[:12] for a in cluster])
print('top wallets', [(a[:12], c) for a, c in deg.most_common(5)])
print('sym n clu clu% rt%')
for r in out:
    print(r)
