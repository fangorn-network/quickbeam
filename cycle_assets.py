import gzip, json, glob
assets = {}
for fp in sorted(glob.glob("cdn/robinhood/shard-*.ndjson.gz")):
    with gzip.open(fp, "rt") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            p = r.get("fields", {})
            if p.get("entityType") != "Asset":
                continue
            sym = p.get("symbol")
            if not sym:
                continue
            cid = (r.get("meta") or {}).get("manifestCid")
            assets[sym] = (
                p.get("holders"), p.get("recentVolume"), p.get("recentTransfers"),
                p.get("price"), p.get("sector"), cid,
            )  # latest shard wins
for sym in sorted(assets):
    h, v, rt, pr, sec, cid = assets[sym]
    print("%-6s holders=%-6s recentVol=%-10s recentXfer=%-6s price=%-8s %-14s cid=%s" % (
        sym, h, v, rt, pr, sec, cid))
print("total assets:", len(assets))
