import gzip, json, glob

found = 0
for path in sorted(glob.glob('cdn/robinhood/shard-*.ndjson.gz')):
    with gzip.open(path, 'rt') as f:
        for line in f:
            r = json.loads(line)
            if r.get('fields', {}).get('entityType') == 'Asset':
                r2 = {k: v for k, v in r.items() if k != 'embedding'}
                print(json.dumps(r2, indent=2)[:2000])
                found += 1
                break
    if found:
        break

print('--- manifest ---')
d = json.load(open('cdn/robinhood/manifest.json'))
print(list(d.keys()))
shards = d.get('shards', [])
if shards:
    print(json.dumps(shards[0], indent=2)[:800])
