import gzip, json, glob, collections, time, os

records = []
for path in sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz')):
    for line in gzip.open(path, 'rt'):
        line = line.strip()
        if line:
            records.append(json.loads(line))

transfers = [r for r in records if r['fields'].get('entityType') == 'Transfer']
asset_recs = [r for r in records if r['fields'].get('entityType') == 'Asset']
MINT = '0x0000000000000000000000000000000000000000'


def endpoints(tx):
    c = collections.Counter()
    for r in tx:
        f = r if isinstance(r, dict) and 'fromAddr' in r else r['fields']
        for k in ('fromAddr', 'toAddr'):
            a = (f.get(k) or '').lower()
            if a:
                c[a] += 1
    return c


# derive primary ring empirically: top-4 wallet endpoints across the whole corpus
ep = endpoints(transfers)
total_endpoints = sum(ep.values())
ring = [a for a, _ in ep.most_common(4)]
ring_set = set(ring)
ring_endpoints = sum(ep[a] for a in ring)
ring_pct_all = 100.0 * ring_endpoints / total_endpoints

# per-symbol flow vs primary ring
per = collections.defaultdict(lambda: {'n': 0, 'ring': 0})
for r in transfers:
    f = r['fields']
    sym = f.get('symbol')
    if not sym:
        continue
    d = per[sym]
    d['n'] += 1
    fa = (f.get('fromAddr') or '').lower()
    ta = (f.get('toAddr') or '').lower()
    if fa in ring_set or ta in ring_set:
        d['ring'] += 1

liquid = sorted(s for s in per if per[s]['n'] >= 1000)
thin = [s for s in per if per[s]['n'] < 1000]

# derive secondary (farming) ring empirically from the thin-symbol transfer set
thin_tx = [r['fields'] for r in transfers if r['fields'].get('symbol') in set(thin)]
sec = [a for a, _ in endpoints(thin_tx).most_common(4)]
sec_set = set(sec) | {MINT}
sec_touch = sum(1 for f in thin_tx
                if (f.get('fromAddr') or '').lower() in sec_set or (f.get('toAddr') or '').lower() in sec_set)
sec_syms = len(set(f['symbol'] for f in thin_tx
                   if (f.get('fromAddr') or '').lower() in sec_set or (f.get('toAddr') or '').lower() in sec_set))
sec_pct = 100.0 * sec_touch / len(thin_tx)
sec_short = '/'.join(a[:10] + '…' for a in sec)

# per-asset record: last occurrence in publish order = latest snapshot.
# (Asset records carry no timestamp/block field; shard append order is publish order.)
asset = {}
for r in asset_recs:
    sym = r['fields'].get('symbol')
    if sym:
        asset[sym] = r

manifest = json.load(open('cdn/robinhood/manifest.json'))
created_at = manifest['created_at']
generated_at = int(time.time())
expires_at = generated_at + 2700
stale_days = (generated_at - created_at) / 86400.0
ring_short = ', '.join(a[:10] + '…' for a in ring)

signals = {}
for sym in liquid:
    d = per[sym]
    pct = 100.0 * d['ring'] / d['n']
    rec = asset.get(sym, {})
    fields = rec.get('fields', {})
    cid = (rec.get('meta') or {}).get('manifestCid')
    owner = rec.get('owner')
    reason = (
        f"flat (wash-trading exclusion): {pct:.1f}% of {sym}'s {d['n']} on-chain transfers touch the "
        f"corpus-wide top-4 wallet ring ({ring_short}), which round-trips funds among itself "
        f"({ring_endpoints}/{total_endpoints} = {ring_pct_all:.1f}% of ALL transfer endpoints). "
        f"Every liquid symbol (n_tx>=1000) is primary-ring-dominated; the {len(thin)} non-ring symbols are each "
        f"too thin (<=147 transfers, 4-12 holders) AND themselves farmed by a secondary ring "
        f"({sec_short} + 0x000 mint) touching {sec_syms}/{len(thin)} of them "
        f"({sec_pct:.0f}% of their transfers) -- no clean long thesis survives this cycle. "
        f"holders={fields.get('holders')}, price={fields.get('price')}, sector={fields.get('sector')}. "
        f"DEGRADED CYCLE: quickbeam MCP(8765)/CDN(8090) daemons unreachable (no listener on either port; "
        f"refresh/search/aggregate/neighbors/get never attached), so this is a direct rebuild of the on-disk "
        f"CDN snapshot, stale (manifest created_at={created_at}, ~{stale_days:.1f}d old, byte-identical since Jul 5). "
        f"No per-record provenance.source_cid in snapshot; cited is this Asset record's own latest-snapshot "
        f"meta.manifestCid={cid} (publisher owner={owner})."
    )
    signals[sym] = {
        'side': 'flat',
        'confidence': 0.0,
        'reason': reason,
        'generated_at': generated_at,
        'expires_at': expires_at,
    }

out = '/home/driemworks/fangorn/robinhood-bot/signals/signals.json'
tmp = out + '.tmp'
with open(tmp, 'w') as fh:
    json.dump(signals, fh, indent=2, sort_keys=True)
os.replace(tmp, out)

# self-check: bimodal structure + all-flat invariant + no clean survivor
assert len(liquid) == 24, liquid
assert all(per[s]['n'] <= 200 for s in thin)
assert all(v['side'] == 'flat' and v['confidence'] == 0.0 for v in signals.values())
assert all(per[s]['ring'] / per[s]['n'] > 0.30 for s in liquid), "a liquid symbol is not ring-dominated"
assert sec_syms == len(thin), "not every thin symbol is farmed by the secondary ring"
print(f"primary ring top-4 = {ring_pct_all:.1f}% of {total_endpoints} endpoints")
print(f"secondary ring+mint = {sec_pct:.1f}% of thin transfers, {sec_syms}/{len(thin)} thin symbols")
print(f"wrote {len(signals)} flat (24 liquid); omitted {len(thin)} thin; "
      f"distinct asset CIDs cited: {len(set((asset[s].get('meta') or {}).get('manifestCid') for s in liquid))}")
print(f"generated_at={generated_at} expires_at={expires_at} (+2700s)")
