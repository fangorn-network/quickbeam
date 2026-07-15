"""test_mcp_analytics.py — the analytical axis (aggregate / export) over a
synthetic in-memory dataset. No network: builds a _Dataset by hand and drives the
pure helpers. Run directly: `python quickbeam/test_mcp_analytics.py`."""
from __future__ import annotations

import json
import os
import sys

from quickbeam import mcp_server as m


def _mk_dataset() -> "m._Dataset":
    """Three wallets moving two symbols — a tiny stand-in for the transfer log."""
    ds = m._Dataset("toy", {"name": "toy", "dim": 4, "model": "x"})
    rows = [
        # symbol, from, to, value
        ("AAA", "0xa", "0xb", 10.0),
        ("AAA", "0xb", "0xa", 10.0),   # circular
        ("AAA", "0xa", "0xc", 5.0),
        ("BBB", "0xa", "0xb", 2.0),
        ("BBB", "0xc", "0xb", None),   # missing value → skipped in sums
    ]
    for i, (sym, fr, to, val) in enumerate(rows):
        ds.records.append({
            "id": f"t{i}", "entityType": "Transfer", "owner": None,
            "fields": {"entityType": "Transfer", "symbol": sym,
                       "fromAddr": fr, "toAddr": to, "value": val},
            "meta": {},
        })
    # one Asset record, to prove entity_type filtering excludes it
    ds.records.append({
        "id": "aAAA", "entityType": "Asset", "owner": None,
        "fields": {"entityType": "Asset", "symbol": "AAA", "price": 100.0, "holders": 7},
        "meta": {},
    })
    return ds


def check(name: str, cond: bool) -> bool:
    print(("  ✓ " if cond else "  ✗ ") + name)
    return cond


def main() -> int:
    ds = _mk_dataset()
    ok = True

    # --- aggregate: legs + token volume per sender, Transfers only ---
    r = m._do_aggregate(ds, "fromAddr", {"legs": "count", "tok": "sum:value"},
                        "Transfer", None, "-legs", 10)
    by = {row["fromAddr"]: row for row in r["rows"]}
    ok &= check("groups over senders = 3 (Asset excluded)", r["groups"] == 3)
    ok &= check("0xa: 3 legs, 17.0 tokens", by["0xa"]["legs"] == 3 and by["0xa"]["tok"] == 17.0)
    ok &= check("0xc: 1 leg, None-value skipped → 0.0", by["0xc"]["legs"] == 1 and by["0xc"]["tok"] == 0.0)
    ok &= check("ordered by -legs (0xa first)", r["rows"][0]["fromAddr"] == "0xa")

    # --- where predicate: only AAA with value >= 5 ---
    r2 = m._do_aggregate(ds, "symbol", {"n": "count", "sum": "sum:value"},
                         "Transfer", {"symbol": "AAA", "value": {"gte": 5}}, None, 10)
    ok &= check("where(symbol=AAA, value>=5) → 3 rows summing 25.0",
                r2["rows"][0]["n"] == 3 and r2["rows"][0]["sum"] == 25.0)

    # --- distinct + min/max ---
    r3 = m._do_aggregate(ds, "symbol",
                         {"peers": "distinct:toAddr", "hi": "max:value", "lo": "min:value"},
                         "Transfer", None, None, 10)
    aaa = {row["symbol"]: row for row in r3["rows"]}["AAA"]
    ok &= check("AAA distinct receivers = 3 (0xb,0xa,0xc)", aaa["peers"] == 3)
    ok &= check("AAA max=10.0 min=5.0", aaa["hi"] == 10.0 and aaa["lo"] == 5.0)

    # --- entity_type filter on aggregate reaches the Asset ---
    r4 = m._do_aggregate(ds, "symbol", {"h": "max:holders"}, "Asset", None, None, 10)
    ok &= check("Asset aggregate sees holders=7", r4["rows"][0]["h"] == 7.0)

    # --- export: projected ndjson to a local file ---
    rows = [m._project_record(rec, ["symbol", "fromAddr", "value"])
            for rec in m._iter_records(ds, "Transfer", None)]
    path, nbytes, count = m._write_rows(rows, "ndjson", "toy-Transfer")
    exists = os.path.exists(path)
    first = json.loads(open(path).readline()) if exists else {}
    ok &= check("export wrote 5 transfer rows", count == 5 and nbytes > 0)
    ok &= check("projected row is flat {id,entityType,symbol,fromAddr,value}",
                set(first) == {"id", "entityType", "symbol", "fromAddr", "value"})
    if exists:
        os.remove(path)

    # --- csv export ---
    p2, b2, c2 = m._write_rows(rows[:2], "csv", "toy-sample")
    head = open(p2).readline().strip() if os.path.exists(p2) else ""
    ok &= check("csv header = id,entityType,symbol,fromAddr,value",
                head == "id,entityType,symbol,fromAddr,value")
    if os.path.exists(p2):
        os.remove(p2)

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
