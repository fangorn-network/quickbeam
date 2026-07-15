# Playground publish service

A ~300-line `node:http` wrapper over `@fangorn-network/sdk`. It owns the operations
that need the publisher private key + Pinata JWT (so they never run in the browser):
schema registration and git-native bundle publishing into the real embedding
pipeline.

## Run

```sh
cd playground/server
npm install                      # links the local SDK (../../../fangorn)

export DELEGATOR_ETH_PRIVATE_KEY=0x...   # publisher key, needs testnet ETH
export PINATA_JWT=...
export PINATA_GATEWAY=https://your-gateway.mypinata.cloud
export CHAIN_NAME=arbitrumSepolia        # or baseSepolia
npm start                                # → http://localhost:8791
```

Env quirks it tolerates (so the same `.env` that drives the Python pipeline works):
a stray leading `.` on `PINATA_JWT`, and a trailing `/ipfs` on `PINATA_GATEWAY` (the
TS SDK appends `/ipfs/<cid>` itself, so a gateway ending in `/ipfs` double-paths and
404s).

Without the env vars it still starts and answers `/health` with `configured:false`.

State (per-schema ids + per-dataset HEAD and full record set) is kept in
`.state.json`. If that's lost but the chain has a tip, the dataset is reconstructed
from IPFS on the next publish.

## Endpoints

| Method + path            | Purpose                                                     |
|--------------------------|-------------------------------------------------------------|
| `GET  /health`           | configured? owner, chain, collection                        |
| `GET  /schemas`          | schemas registered by this server                           |
| `GET  /schema/summon`    | `?name=` → fetch a schema from the on-chain registry        |
| `POST /schema/register`  | `{ name, type, fields }` → node schema + bundle schema + watch cmd |
| `POST /publish`          | `{ schemaName, records }` → `commitBundle` (full snapshot) + `push` |
| `GET  /published`        | current full record set per dataset + commit history        |

Publishing shapes flat records into a single-node bundle with self-edges so the
`quickbeam watch --bundle` daemon (which edge-walks and skips edge-less manifests)
ingests them. See the top-level README for the full flow.
