import gzip, json, glob

records = []
for path in sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz')):
    with gzip.open(path, 'rt') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get('fields', {}).get('entityType') == 'Transfer':
                records.append(r['fields'])

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

total = len(records)
ring2_touch = 0
for f in records:
    fa = (f.get('fromAddr') or '').lower()
    ta = (f.get('toAddr') or '').lower()
    if fa in RING2 or ta in RING2:
        ring2_touch += 1

print(f"total transfers={total}")
print(f"ring2 touch={ring2_touch} ({100*ring2_touch/total:.1f}%)")

# check 0x8366a39... bridges both rings - which symbols does it touch overall?
import collections
bridge = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
syms = collections.Counter()
for f in records:
    fa = (f.get('fromAddr') or '').lower()
    ta = (f.get('toAddr') or '').lower()
    if fa == bridge or ta == bridge:
        syms[f.get('symbol')] += 1
print("bridge wallet 0x8366a39... touches symbols:", dict(syms))
