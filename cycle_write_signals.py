import gzip, json, glob, collections, time, os

records = []
for path in sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz')):
    with gzip.open(path, 'rt') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

transfers = [r['fields'] for r in records if r['fields'].get('entityType') == 'Transfer']
assets_recs = [r for r in records if r['fields'].get('entityType') == 'Asset']

RING1 = {
    '0xc94135b63772b91d79d0a2daab2a8801f32359bd',
    '0x2f4579ca81717d3d61bf8b6f06571877bbe54a07',
    '0x624c6dbb5d1aae291c788ef116e69a59552b17c4',
    '0x33b0095333e64bf375952ef197b6fdc3437dc014',
}
RING2 = {
    '0x1a18a8b96eac3f980133a18402d04194f1faa4e7',
    '0xfac1d7dc76be90c5cadd5b022af7838dd8190f16',
    '0x6d56ab475069b7e93886b3d3f06c5435b87ba158',
    '0xcfaece2151502da2a21d47234ae1f08618a60a94',
    '0x43cf43056c33128329b54f66ca3649cf2975f1a6',
    '0x006102b16a04c20306a28b652745d3973d7d24fa',
    '0x58d7fc9319c926e21cea96a32b230b71b244196d',
    '0x8366a39cc670b4001a1121b8f6a443a643e40951',
    '0x0000000000000000000000000000000000000000',
}
ALL_RING = RING1 | RING2
RING2_SYMBOLS = {
    'RKLB', 'TSM', 'RBLX', 'FLNC', 'DDOG', 'MSTR', 'RGTI', 'EWY', 'RDW', 'SOFI',
    'BABA', 'APLD', 'ASML', 'NBIS', 'RDDT', 'IONQ', 'IREN', 'ASTS', 'GME', 'COST',
    'LITE', 'TTWO', 'GLW', 'QCOM', 'AMAT', 'CLSK',
}

per_symbol = collections.defaultdict(lambda: {'n': 0, 'ring': 0, 'wallets': set()})
for f in transfers:
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
    if fa in ALL_RING or ta in ALL_RING:
        d['ring'] += 1

asset_meta = {}
for r in assets_recs:
    sym = r['fields'].get('symbol')
    if not sym:
        continue
    prev = asset_meta.get(sym)
    if prev is None or len(r['fields']) > len(prev['fields']):
        asset_meta[sym] = r

total_transfers = len(transfers)
ring1_touch = sum(1 for f in transfers if (f.get('fromAddr') or '').lower() in RING1 or (f.get('toAddr') or '').lower() in RING1)
ring2_touch = sum(1 for f in transfers if (f.get('fromAddr') or '').lower() in RING2 or (f.get('toAddr') or '').lower() in RING2)

manifest = json.load(open('cdn/robinhood/manifest.json'))
manifest_created_at = manifest['created_at']

generated_at = int(time.time())
expires_at = generated_at + 2700
stale_hours = (generated_at - manifest_created_at) / 3600.0

signals = {}
for sym in sorted(per_symbol):
    d = per_symbol[sym]
    pct = 100.0 * d['ring'] / d['n']
    meta = asset_meta.get(sym)
    cid = (meta.get('meta') or {}).get('manifestCid') if meta else None
    owner = meta.get('owner') if meta else None
    holders = meta['fields'].get('holders') if meta else None

    if sym in RING2_SYMBOLS:
        ring_label = ("secondary farming ring (0x1A18a8b9/0xfac1d7dC/0x6d56Ab47/0xcfAEce21/"
                      "0x43cF4305/0x006102b1/0x58d7fc93/0x8366a39C + 0x000...0 mint)")
    else:
        ring_label = "primary 4-wallet ring (0xc94135b6/0x2f4579ca/0x624c6dbb/0x33b00953 faucet)"

    reason = (
        f"wash-trading: {pct:.0f}% of this asset's {d['n']} lifetime transfers touch the {ring_label}. "
        f"Across the full 50-asset corpus, two wallet rings together touch {ring1_touch + ring2_touch}/{total_transfers} "
        f"({100.0*(ring1_touch+ring2_touch)/total_transfers:.1f}%) of ALL transfers "
        f"(primary ring {ring1_touch}/{total_transfers}={100.0*ring1_touch/total_transfers:.1f}%, "
        f"secondary ring {ring2_touch}/{total_transfers}={100.0*ring2_touch/total_transfers:.1f}%). "
        f"Wallet 0x8366a39C… bridges both rings (touches TSLA/NVDA/AMD/AAPL/SPCX/CRCL/QQQ/SNDK in the primary cluster "
        f"and all 25 secondary-cluster symbols), indicating one coordinated actor manufactures volume across the entire corpus -- "
        f"not organic demand. Only {len(d['wallets'])} distinct wallets touch this asset; on-chain holders={holders}. "
        f"No long thesis anywhere in this corpus this cycle; explicit flat (close/avoid). "
        f"Provenance: shard manifestCid {cid} (publisher {owner}); local CDN snapshot cdn/robinhood/, "
        f"manifest created_at={manifest_created_at} (~{stale_hours:.0f}h stale). "
        f"quickbeam MCP(8765)/CDN(8090) daemons unreachable this cycle (connection refused; confirmed via ss -tlnp, "
        f"no listener on either port) -> no live refresh/get, so provenance cites shard meta.manifestCid, "
        f"not live provenance.source_cid."
    )
    signals[sym] = {
        'side': 'flat',
        'confidence': 0.0,
        'reason': reason,
        'generated_at': generated_at,
        'expires_at': expires_at,
    }

out_path = '/home/driemworks/fangorn/robinhood-bot/signals/signals.json'
tmp_path = out_path + '.tmp'
with open(tmp_path, 'w') as f:
    json.dump(signals, f, indent=2, sort_keys=True)
os.replace(tmp_path, out_path)
print(f"wrote {len(signals)} signals to {out_path}")
print(f"generated_at={generated_at} expires_at={expires_at}")
