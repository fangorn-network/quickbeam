import json, os, time

SIG_DIR = "/home/driemworks/fangorn/robinhood-bot/signals"
now = int(time.time())
expires = now + 2700  # 45 min = 3x the 15-min cadence

signals = {
    "AAPL": {
        "side": "flat",
        "confidence": 0.0,
        "reason": (
            "wash-trading confirmed: on-chain flow dominated by a circular wallet cluster "
            "0xC94135b6<->0x2F4579Ca<->0x624C6Dbb -- bidirectional reused edges of "
            "311/171 and 274/137 transfers with matched round-trip values (3.01 out/3.01 back, "
            "0.63, 0.49, ...); 94% of the 1716 transfers ride reused (from,to) edges, "
            "top-2 wallets = 65% of outflow, plus 865 zero-value and 343 repeated 0.0001-token "
            "transfers. recentVolume 24.4 and 255 'holders' are a manufactured veneer, not "
            "organic demand -> avoid/close. "
            "source_cid=bafkreig5cibcrxi3sxu7xe5rlog7dozhrw2faybxvcrm3bo2xg43wdy5li"
        ),
        "generated_at": now,
        "expires_at": expires,
    }
}

tmp = os.path.join(SIG_DIR, "signals.json.tmp")
dst = os.path.join(SIG_DIR, "signals.json")
with open(tmp, "w") as f:
    json.dump(signals, f, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, dst)
print("wrote", dst, "now=", now, "expires=", expires)
print(open(dst).read())
