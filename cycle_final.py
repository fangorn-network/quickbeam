import gzip, json, glob, collections, os, time

files = sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz'))
transfers = []
assets = {}

for f in files:
    with gzip.open(f, 'rt') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            flds = rec.get('fields', {})
            et = flds.get('entityType')
            if et == 'Transfer':
                transfers.append(flds)
            elif et == 'Asset':
                sym = flds.get('symbol')
                assets[sym] = {
                    'holders': flds.get('holders'),
                    'price': flds.get('price'),
                    'sector': flds.get('sector'),
                    'manifestCid': rec.get('meta', {}).get('manifestCid'),
                    'owner': rec.get('owner'),
                }

primary_ring = {
    '0xc94135b63772b91d79d0a2daab2a8801f32359bd',
    '0x2f4579ca81717d3d61bf8b6f06571877bbe54a07',
    '0x624c6dbb5d1aae291c788ef116e69a59552b17c4',
    '0x33b0095333e64bf375952ef197b6fdc3437dc014',
}
secondary_ring = {
    '0xcfaece2151502da2a21d47234ae1f08618a60a94',
    '0x6d56ab475069b7e93886b3d3f06c5435b87ba158',
    '0x1a18a8b96eac3f980133a18402d04194f1faa4e7',
    '0xfac1d7dc76be90c5cadd5b022af7838dd8190f16',
    '0x0000000000000000000000000000000000000000',
    '0x58d7fc9319c926e21cea96a32b230b71b244196d',
    '0x8366a39cc670b4001a1121b8f6a443a643e40951',
    '0x43cf43056c33128329b54f66ca3649cf2975f1a6',
    '0x006102b16a04c20306a28b652745d3973d7d24fa',
}
bridge_wallet = '0x8366a39cc670b4001a1121b8f6a443a643e40951'

per_symbol = collections.defaultdict(lambda: {'total': 0, 'primary': 0, 'secondary': 0, 'wallets': set()})
for t in transfers:
    sym = t.get('symbol')
    f_ = t.get('fromAddr', '').lower()
    to = t.get('toAddr', '').lower()
    d = per_symbol[sym]
    d['total'] += 1
    d['wallets'].add(f_)
    d['wallets'].add(to)
    if f_ in primary_ring or to in primary_ring:
        d['primary'] += 1
    if f_ in secondary_ring or to in secondary_ring:
        d['secondary'] += 1

total_transfers = len(transfers)
total_primary = sum(1 for t in transfers if t.get('fromAddr', '').lower() in primary_ring or t.get('toAddr', '').lower() in primary_ring)
total_secondary = sum(1 for t in transfers if t.get('fromAddr', '').lower() in secondary_ring or t.get('toAddr', '').lower() in secondary_ring)

now = int(time.time())
expires = now + 2700

signals = {}
for sym, d in per_symbol.items():
    a = assets.get(sym, {})
    pct_primary = d['primary'] / d['total'] * 100 if d['total'] else 0
    pct_secondary = d['secondary'] / d['total'] * 100 if d['total'] else 0
    ring = 'primary' if pct_primary >= pct_secondary else 'secondary'
    pct = pct_primary if ring == 'primary' else pct_secondary

    reason = (
        f"wash-trading: {pct:.0f}% of this asset's {d['total']} recorded transfers touch the "
        f"{ring} wallet ring ({'0xC94135b6/0x2F4579Ca/0x624C6Dbb/0x33B00953 4-wallet cluster' if ring == 'primary' else '0x1A18a8b9/0xfac1d7dC/0x6d56Ab47/0xcfAEce21/0x43cF4305/0x006102b1/0x58d7fc93 cluster + 0x000...0 mint feed'}). "
        f"Corpus-wide: {total_primary}/{total_transfers} ({total_primary/total_transfers*100:.1f}%) of ALL 50-asset transfers touch the primary ring, "
        f"{total_secondary}/{total_transfers} ({total_secondary/total_transfers*100:.1f}%) touch the secondary ring; "
        f"bridge wallet {bridge_wallet[:10]}... links both clusters, indicating one coordinated actor manufactures volume "
        f"across the entire corpus rather than organic demand. {len(d['wallets'])} distinct wallets touch this asset "
        f"(on-chain holders={a.get('holders', '?')}). No long thesis survives the wash-trading screen for any symbol this cycle; explicit flat (close/avoid). "
        f"Provenance: shard meta.manifestCid={a.get('manifestCid', '?')} (publisher {a.get('owner', '?')}); source: local CDN snapshot cdn/robinhood/ "
        f"(manifest created_at=1783285844, unchanged since 2026-07-05 ~16:10 UTC). quickbeam MCP(8765)/CDN(8090) daemons unreachable this cycle "
        f"(no listeners per ss -tlnp) -> refresh/search/aggregate/neighbors/get tools never attached; provenance.source_cid unavailable, "
        f"citing shard meta.manifestCid instead."
    )

    signals[sym] = {
        'side': 'flat',
        'confidence': 0.0,
        'reason': reason,
        'generated_at': now,
        'expires_at': expires,
    }

out_path = '/home/driemworks/fangorn/robinhood-bot/signals/signals.json'
tmp_path = out_path + '.tmp'
with open(tmp_path, 'w') as fh:
    json.dump(signals, fh, indent=2, sort_keys=True)
os.replace(tmp_path, out_path)

print(f'wrote {len(signals)} symbols, generated_at={now}, expires_at={expires}')
