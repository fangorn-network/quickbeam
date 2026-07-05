import csv, collections

ZERO = "0x0000000000000000000000000000000000000000"
path = "/tmp/quickbeam-exports/robinhood-Transfer-ad0d25.csv"

rows = collections.defaultdict(list)
with open(path) as f:
    for r in csv.DictReader(f):
        sym = r["symbol"] or "?"
        rows[sym].append(r)

def num(x):
    try: return float(x)
    except: return 0.0

print(f"{'SYM':6} {'n':>5} {'wallets':>7} {'mintN':>5} {'top2wshr':>8} {'roundfrac':>9} {'cycles':>6} {'edgeReuse':>9}")
summary = {}
for sym, trs in sorted(rows.items(), key=lambda kv: -len(kv[1])):
    n = len(trs)
    # wallet activity
    out_ct = collections.Counter()
    edges = collections.Counter()      # (from,to) pair reuse
    values = collections.Counter()
    wallets = set()
    mint = 0
    adj = collections.defaultdict(set)
    for r in trs:
        fa, ta, v = r["fromAddr"], r["toAddr"], r["value"]
        wallets.add(fa); wallets.add(ta)
        if fa == ZERO: mint += 1
        out_ct[fa] += 1
        edges[(fa, ta)] += 1
        values[round(num(v), 6)] += 1
        adj[fa].add(ta)
    # rounded-value fraction: values that are integer-ish and repeat
    rounded = sum(c for val, c in values.items() if val == int(val) and c >= 3 and val != 0)
    round_frac = rounded / n if n else 0
    # top-2 wallet share of outgoing transfers
    top2 = sum(c for _, c in out_ct.most_common(2))
    top2_share = top2 / n if n else 0
    # edge reuse: fraction of transfers on a (from,to) pair seen >=3 times
    reused = sum(c for e, c in edges.items() if c >= 3)
    edge_reuse = reused / n if n else 0
    # short cycle detection A->B->...->A up to len 4
    cycles = 0
    nodes = list(adj.keys())
    def find_cycle(start, cur, depth, seen):
        global cycles
        if depth > 4: return
        for nxt in adj.get(cur, ()):
            if nxt == start and depth >= 2:
                cycles += 1; return
            if nxt not in seen and nxt != ZERO:
                find_cycle(start, nxt, depth+1, seen | {nxt})
    # limit cycle search to the busiest 40 source wallets for tractability
    for s in [w for w, _ in out_ct.most_common(40)]:
        if s == ZERO: continue
        find_cycle(s, s, 0, {s})
    summary[sym] = dict(n=n, wallets=len(wallets), mint=mint, top2=top2_share,
                        round_frac=round_frac, cycles=cycles, edge_reuse=edge_reuse)
    print(f"{sym:6} {n:5d} {len(wallets):7d} {mint:5d} {top2_share:8.2f} {round_frac:9.2f} {cycles:6d} {edge_reuse:9.2f}")

# Detail on AAPL: most reused edges + most common rounded values
print("\n=== AAPL edge/value detail ===")
aapl = rows.get("AAPL", [])
edges = collections.Counter(); values = collections.Counter(); outc = collections.Counter()
for r in aapl:
    edges[(r["fromAddr"], r["toAddr"])] += 1
    values[r["value"]] += 1
    outc[r["fromAddr"]] += 1
print("top reused (from,to) edges:")
for e, c in edges.most_common(8):
    print(f"  {c:4d}x  {e[0][:12]}..->{e[1][:12]}..")
print("top repeated values:")
for v, c in values.most_common(8):
    print(f"  {c:4d}x  value={v}")
print("top outgoing wallets:")
for w, c in outc.most_common(6):
    print(f"  {c:4d}x  {w[:16]}  ({'MINT/0x0' if w==ZERO else ''})")
