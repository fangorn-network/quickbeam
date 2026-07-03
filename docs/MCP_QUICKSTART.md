# Quickstart — query a live knowledge mesh from an agent

How an agent (Claude, an autonomous trading agent, a mobile assistant) connects to a
Fangorn dataset and navigates it over MCP. This is the **consumption** side; for the
**publish** side (how the data gets on-chain and into the CDN) see
[`NEW_QUICKSTART.md`](./NEW_QUICKSTART.md).

The running example is `robinhood` — a tokenized-stock universe (tokenized Assets +
their on-chain Transfers, corporate actions, oracle updates, news).

---

## The model in one screen

Three processes, two of them long-running:

```
                    publish on-chain (see NEW_QUICKSTART)
                                 │
                                 ▼
   quickbeam watch  ──┬─▶ embed → Qdrant → CDN record shards   (SEMANTIC axis)
   (live daemon)      └─▶ fetch edges     → CDN edges.json      (RELATIONAL axis)
                                 │
                                 ▼
   quickbeam cdn serve  ── static shards + /edges over HTTP (:8090)
                                 │  pull (immutable, verified)
                                 ▼
   quickbeam mcp  ── in-process index, embeds queries LOCALLY (:8765)  ◀── your agent
```

Two things make this different from a normal search API:

- **The MCP is a local pull-client.** It pulls a dataset's shards into memory and
  embeds your query **in-process** — the query vector never leaves the box. *Knowledge
  is public; intent is private.* There is no central search server to leak to.
- **Two navigation axes.** `search` (vector similarity) *and* `neighbors` (typed graph
  edges). An agent both finds records by meaning **and** walks their relationships.

---

## 0. Setup (once)

```bash
cd ~/fangorn/embeddings
python -m venv venv && source venv/bin/activate
pip install -e ".[agent]"     # base (numpy, fastembed, qdrant) + fastmcp + httpx + eth-account
```

You need a **baked CDN directory** to serve. Either run the live watcher (§2) to build
it from on-chain, or use an existing `./cdn` (the repo ships one for `robinhood`).

---

## 1. Serve the CDN

The CDN is a static file server for the immutable shards plus the linkset.

```bash
quickbeam cdn serve --cdn-dir ./cdn --port 8090 --cors
# [serve] Semantic CDN on http://0.0.0.0:8090 (dir: .../cdn)
```

Sanity check both axes are being served:

```bash
curl -s localhost:8090/catalog | jq '.domains[] | {name, count}'
curl -s localhost:8090/domains/robinhood/edges | jq '{count, relations}'
```

> **Gotcha:** if `/domains/robinhood/edges` returns **404** but `cdn/robinhood/edges.json`
> exists, your `cdn serve` process is older than the `/edges` route — **restart it**.

---

## 2. Run the live stream (optional — skip if `./cdn` is already baked)

`quickbeam watch` is the daemon that turns on-chain publishes into CDN deltas. Each
cycle it embeds new records into a delta shard **and** merges newly-fetched typed edges
into `edges.json` — so both axes stay fresh with the stream, no manual step.

```bash
./watch_robinhood.sh          # needs quickbeam/.env with BUILD_AUTH
# [Watcher] Cycle 1 complete — 50 new records embedded (last block …)
# [Watcher] CDN edges: +250 new (250 total; relations=['hasTransfer'])
```

To push *new* data into the stream (upstream of `watch`), see
[`ROBINHOOD.md`](./ROBINHOOD.md): `quickbeam data robinhood …` → `fangorn commit --bundle`
→ `fangorn push`. The watcher picks it up on the next poll.

---

## 3. Start the MCP

```bash
# remote streamable-http (agents connect over the network):
quickbeam mcp --transport http --host 0.0.0.0 --port 8765 --cdn-url http://localhost:8090

# local stdio (MCP Inspector / Claude Desktop):
quickbeam mcp --transport stdio --cdn-url http://localhost:8090
```

It logs `[mcp] Phase 1 — tools are free`. (Phase 2 charges per call — see
[README §x402](../README.md#x402-payment-gating).)

---

## 4. The five tools

Discovery tools (`list_datasets`, `describe`, `get`) are free; `search` and `neighbors`
are the compute-bearing ones.

| Tool | Use it to… |
|---|---|
| `list_datasets()` | see what datasets exist (name, count, entity types) |
| `describe(dataset)` | learn a dataset's fields, relation types, and embedding model |
| `search(dataset, query, limit=10, entity_type=None, owner=None)` | find records by **meaning** |
| `get(dataset, id)` | fetch one record fully (its `id` is also its graph node) |
| `neighbors(dataset, id, rel=None, direction="both", limit=25)` | **walk the graph** from a node |

A natural agent flow — *"find AI-chip assets, then see NVIDIA's on-chain activity"*:

```jsonc
// 1. discover
list_datasets()
// → { "datasets": [ { "name": "robinhood", "count": 50, "entity_types": ["Asset"] } ] }

// 2. search the semantic axis — returns RAW record fields + on-chain provenance
search("robinhood", "AI chip company making GPUs for data centers", limit=3)
// → { "results": [
//      { "id": "rh:asset:NVDA", "entityType": "Asset",
//        "fields": { "symbol": "NVDA", "name": "NVIDIA", "sector": "Semiconductors", "text": "…" },
//        "score": 0.77,
//        "provenance": { "source_cid": "bafk…", "publisher": "0x…", "version": … } },
//      { "id": "rh:asset:RGTI", … }, { "id": "rh:asset:AMD", … } ] }

// 3. walk the relational axis from that node
neighbors("robinhood", "rh:asset:NVDA", rel="hasTransfer", limit=5)
// → { "neighbors": [
//      { "rel": "hasTransfer", "direction": "out",
//        "id": "rh:xfer:0xe933…:212", "entityType": "Transfer" }, … ] }
```

Notes an agent should know:
- `search` returns the **raw fields**, not a title/subtitle projection — reason over the
  JSON directly.
- Every result carries `provenance` (source CID, publisher, version) — cite it when the
  user cares about origin or freshness.
- A neighbor **inside** the dataset comes back with full `fields`; one **outside** it
  (e.g. a `Transfer` node in an Asset-only corpus) comes back as an `{id, entityType}`
  endpoint you can still reason about.

---

## 5. Register the MCP with Claude Code

```bash
# http transport (server running from §3):
claude mcp add --transport http quickbeam http://localhost:8765/mcp

# or stdio (Claude Code launches it for you):
claude mcp add quickbeam -- quickbeam mcp --transport stdio --cdn-url http://localhost:8090
```

Then in a session the tools appear as `list_datasets`, `search`, `neighbors`, etc.

Config knobs (env or flags): `QUICKBEAM_CDN_URL` (`--cdn-url`), and `QUICKBEAM_EDGES`
(`--edges`) — a local linkset file/dir used only as a fallback when the CDN has no
`/edges` for a dataset.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `neighbors` returns `[]` with `"relational layer not delivered"` | The dataset has no linkset on the CDN. Run the watcher (§2), or `quickbeam cdn edges --domain <d> --source <linkset.json>`, then restart `cdn serve`. |
| `/domains/<d>/edges` → 404 but `edges.json` exists on disk | `cdn serve` predates the `/edges` route — **restart it**. |
| `search` returns the **same** results for every query (identical scores) | Collapsed embeddings — a stale/foreign `db/role_map.json` was applied at embed time, so every record embedded the same empty text. Fixed by the role-map guard; delete `db/role_map.json` and re-run `watch`, or re-bake. |
| `list_datasets` shows a dataset with `count: 0` | It's declared but unbaked; empty datasets are hidden from `list_datasets` by design. |
| MCP errors `dataset_unavailable` | The CDN isn't reachable at `--cdn-url`, or the dataset name is wrong (check `list_datasets`). |

---

## See also

- [README § MCP server](../README.md#mcp-server) — full tool reference + x402 payments.
- [README § Semantic CDN](../README.md#semantic-cdn) — baking, delta shards, edge delivery.
- [`NEW_QUICKSTART.md`](./NEW_QUICKSTART.md) — the git-native publish flow (commit / push).
- [`ROBINHOOD.md`](./ROBINHOOD.md) — the financial dataset used in the examples here.
