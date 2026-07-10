import json, collections
e = json.load(open("cdn/robinhood/edges.json"))
edges = e["edges"]
ring = {
    "rh:wallet:0xc94135b63772b91d79d0a2daab2a8801f32359bd",
    "rh:wallet:0x2f4579ca81717d3d61bf8b6f06571877bbe54a07",
    "rh:wallet:0x624c6dbb5d1aae291c788ef116e69a59552b17c4",
    "rh:wallet:0x33b0095333e64bf375952ef197b6fdc3437dc014",
}
xfer_wallets = collections.defaultdict(set)
asset_xfer = collections.defaultdict(set)
xfers = set()
for ed in edges:
    r, f, t = ed["rel"], ed["from"], ed["to"]
    if r in ("sentBy", "receivedBy"):
        xfers.add(f)
        xfer_wallets[f].add(t)
    elif r == "hasTransfer":
        asset_xfer[f].add(t)
touched = {x for x, ws in xfer_wallets.items() if ws & ring}
print("corpus: %d/%d = %.1f%% transfers touch ring" % (len(touched), len(xfers), 100 * len(touched) / len(xfers)))
rows = []
for a, xs in asset_xfer.items():
    xs2 = [x for x in xs if x in xfer_wallets]
    if not xs2:
        continue
    tr = sum(1 for x in xs2 if xfer_wallets[x] & ring)
    rows.append((a.replace("rh:asset:", ""), len(xs2), 100 * tr / len(xs2)))
for sym, n, p in sorted(rows, key=lambda r: -r[1]):
    print("%-6s n=%4d ring=%3.0f%%" % (sym, n, p))
