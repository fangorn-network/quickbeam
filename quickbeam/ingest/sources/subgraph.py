"""The Graph subgraph queries — resolving dataset manifests from on-chain events.

`block_gt` variants add `blockNumber_gt` to the where clause for incremental polling
in the watcher (avoids re-scanning the full event history each cycle).
"""
import asyncio

import aiohttp
from tqdm import tqdm

_PUBLISHES_Q = "query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!) { manifestPublisheds(where: { schemaId: $schemaId }, first: $first, skip: $skip, orderBy: blockNumber, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
_PUBLISHES_Q_FROM = "query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!, $blockGt: BigInt!) { manifestPublisheds(where: { schemaId: $schemaId, blockNumber_gt: $blockGt }, first: $first, skip: $skip, orderBy: blockNumber, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
_UPDATES_Q = "query Updates($schemaId: Bytes!, $first: Int!, $skip: Int!) { manifestUpdateds(where: { schemaId: $schemaId }, first: $first, skip: $skip, orderBy: version, orderDirection: desc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"
_UPDATES_Q_FROM = "query Updates($schemaId: Bytes!, $first: Int!, $skip: Int!, $blockGt: BigInt!) { manifestUpdateds(where: { schemaId: $schemaId, blockNumber_gt: $blockGt }, first: $first, skip: $skip, orderBy: version, orderDirection: desc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"

# Unfiltered variants — used by the Composed View path (Phase 1). A view names
# its sources by *resourceId* (a hash of owner+schemaId+name), which the subgraph
# does not index, so we page the full ManifestPublished/Updated history and
# recompute each event's resourceId locally (see _identity.resource_id) to keep
# the ones a view asked for.
#
# These page with a KEYSET cursor (`id_gt`), not `skip`: The Graph hard-caps `skip`
# at 5000, and the global history routinely exceeds that (a single sharded publish
# emits thousands of ManifestPublished records). Ordering by `id` asc and advancing
# `id_gt` to the last row's id has no such limit. Consumers select the latest
# manifest per resourceId by comparing blockNumber explicitly, so query order is
# irrelevant here.
_PUBLISHES_ALL_Q = "query Publishes($first: Int!, $lastId: String!) { manifestPublisheds(first: $first, where: { id_gt: $lastId }, orderBy: id, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
_UPDATES_ALL_Q = "query Updates($first: Int!, $lastId: String!) { manifestUpdateds(first: $first, where: { id_gt: $lastId }, orderBy: id, orderDirection: asc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"


async def _query_subgraph_async(url, api_key, query, variables):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with aiohttp.ClientSession() as session:
        for attempt in range(5):
            try:
                async with session.post(url, json={"query": query, "variables": variables}, headers=headers, timeout=30) as resp:
                    if resp.status in {429, 500, 502, 503, 504}: raise Exception()
                    resp.raise_for_status()
                    data = await resp.json()
                    if "errors" in data: raise RuntimeError(data["errors"])
                    return data["data"]
            except Exception:
                if attempt == 4: raise
                await asyncio.sleep(1 + attempt)


async def _fetch_all_events_async(url, api_key, schema_id, page_size, block_gt=None):
    publishes, updates = [], []
    pairs = [
        (publishes, _PUBLISHES_Q_FROM if block_gt is not None else _PUBLISHES_Q, "manifestPublisheds"),
        (updates,   _UPDATES_Q_FROM   if block_gt is not None else _UPDATES_Q,   "manifestUpdateds"),
    ]
    for target, query, key in pairs:
        skip = 0
        pbar = tqdm(desc=f"  ↳ Fetching {key}", unit=" events", leave=False)
        while True:
            variables = {"schemaId": schema_id, "first": page_size, "skip": skip}
            if block_gt is not None:
                variables["blockGt"] = block_gt
            data = await _query_subgraph_async(url, api_key, query, variables)
            batch = data.get(key, [])
            target.extend(batch)
            pbar.update(len(batch))
            if len(batch) < page_size:
                break
            skip += page_size
        pbar.close()
    return publishes, updates


async def _fetch_all_events_global(url, api_key, page_size):
    """Page the *entire* ManifestPublished/Updated history (no schemaId filter).
    Used only by the Composed View path, where sources are resourceIds we must
    match against every datasource rather than a single known schema."""
    publishes, updates = [], []
    pairs = [
        (publishes, _PUBLISHES_ALL_Q, "manifestPublisheds"),
        (updates,   _UPDATES_ALL_Q,   "manifestUpdateds"),
    ]
    for target, query, key in pairs:
        last_id = ""  # keyset cursor: "" is lexicographically smallest → first page
        pbar = tqdm(desc=f"  ↳ Scanning {key}", unit=" events", leave=False)
        while True:
            data = await _query_subgraph_async(url, api_key, query, {"first": page_size, "lastId": last_id})
            batch = data.get(key, [])
            target.extend(batch)
            pbar.update(len(batch))
            if len(batch) < page_size:
                break
            last_id = batch[-1]["id"]  # advance past the last row; no skip cap
        pbar.close()
    return publishes, updates
