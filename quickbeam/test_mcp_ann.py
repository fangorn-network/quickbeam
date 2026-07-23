"""test_mcp_ann.py — the semantic axis (search) over the int8-quantized HNSW ANN
index. No network, no model: builds a _Dataset by hand, finalizes the faiss index,
monkeypatches the query embedder to plant a known vector, and drives the real
`search` tool. Run directly: `python quickbeam/test_mcp_ann.py`."""
from __future__ import annotations

import asyncio
import sys

import faiss
import numpy as np

from quickbeam import mcp_server as m

DIM = 8


def _mk_dataset() -> "m._Dataset":
    """Six records on orthonormal basis vectors e0..e5 — three Assets (owner 0xA),
    three Transfers (owner 0xB). Orthonormal so the nearest neighbour of a planted
    query e_i is unambiguously record i (cosine 1.0), and every other is cosine 0."""
    ds = m._Dataset("toy", {"name": "toy", "dim": DIM, "model": "x"})
    specs = [("Asset", "0xA"), ("Asset", "0xA"), ("Asset", "0xA"),
             ("Transfer", "0xB"), ("Transfer", "0xB"), ("Transfer", "0xB")]
    vecs = []
    for i, (etype, owner) in enumerate(specs):
        ds.records.append({
            "id": f"r{i}", "entityType": etype, "owner": owner,
            "fields": {"entityType": etype}, "meta": {},
        })
        e = np.zeros(DIM, dtype=np.float32)
        e[i] = 1.0
        vecs.append(e.tolist())
    ds.finalize(vecs)
    return ds


def check(name: str, cond: bool) -> bool:
    print(("  ✓ " if cond else "  ✗ ") + name)
    return cond


def main() -> int:
    ds = _mk_dataset()
    m._REGISTRY["toy"] = ds   # so _ensure_loaded returns it without a CDN pull

    # Plant the query vector: "qN" embeds to basis vector eN. Bypasses the model
    # and matryoshka entirely — the query never touches the network.
    async def fake_embed_query(model, dim, text):
        e = np.zeros(DIM, dtype=np.float32)
        e[int(text[1:])] = 1.0
        return e
    m._embed_query = fake_embed_query

    search = getattr(m.search, "fn", m.search)   # unwrap the @mcp.tool
    run = asyncio.run
    ok = True

    # --- structure: it really is an int8-quantized HNSW index over all 6 rows ---
    ok &= check("index is IndexHNSWSQ", isinstance(ds.index, faiss.IndexHNSWSQ))
    ok &= check("index holds all 6 vectors", ds.index.ntotal == 6)

    # --- ANN: planted query e3 → record r3 first, cosine ~1.0 ---
    r = run(search("toy", query="q3", limit=3))
    top = r["results"][0]
    ok &= check("q3 nearest is r3", top["id"] == "r3")
    ok &= check("exact match scores ~1.0", abs(top["score"] - 1.0) < 1e-3)

    # --- entity_type filter rides the ANN as an IDSelector: query sits on an Asset
    #     (e0) but we ask for Transfers → zero Assets come back ---
    rf = run(search("toy", query="q0", limit=10, entity_type="Transfer"))
    types = {x["entityType"] for x in rf["results"]}
    ok &= check("entity_type=Transfer excludes all Assets", types == {"Transfer"})

    # --- owner filter: query e3 is owner 0xB, but restrict to 0xA → r3 excluded,
    #     only the three 0xA records (r0,r1,r2) survive ---
    ro = run(search("toy", query="q3", limit=10, owner="0xA"))
    ids = {x["id"] for x in ro["results"]}
    ok &= check("owner=0xA returns exactly the 0xA records", ids == {"r0", "r1", "r2"})

    # --- empty filter → no scan, empty results (the run=False path) ---
    re = run(search("toy", query="q0", limit=5, entity_type="Nonexistent"))
    ok &= check("unmatched filter → empty results", re["results"] == [])

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
