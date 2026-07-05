"""IPFS resolution — turn on-chain CIDs into JSON payloads, fetched concurrently.

`_cid_to_path` normalises the several CID encodings the chain hands us (raw base58,
`ipfs://` URIs, 0x-hex multihash) into the path segment a gateway expects.
"""
import asyncio
import json

import aiohttp
from tqdm import tqdm

_B58_ALPHABET = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def _b58encode(v: bytes) -> str:
    leading = len(v) - len(v.lstrip(b'\x00'))
    n = int.from_bytes(v, 'big')
    res = []
    while n:
        n, r = divmod(n, 58)
        res.append(_B58_ALPHABET[r])
    return ('1' * leading) + bytes(reversed(res)).decode('ascii')


def _cid_to_path(cid: str) -> str:
    # Chunk dataCids are stored as full `ipfs://<dirCid>/<file>` URIs (UnixFS dir +
    # path); strip the scheme so the gateway URL is `<gw>/ipfs/<dirCid>/<file>` and
    # not the malformed `<gw>/ipfs/ipfs://<dirCid>/<file>` (→ 400).
    if cid.startswith("ipfs://"):
        cid = cid[len("ipfs://"):]
    if cid.startswith(('0x', '0X')):
        raw = bytes.fromhex(cid[2:])
        if len(raw) == 34 and raw[0] == 0x12 and raw[1] == 0x20:
            return _b58encode(raw)
    return cid


async def _fetch_json(session, sem, url, timeout, pbar):
    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    res = json.loads(await resp.text())
                    pbar.update(1)
                    return res
                print(f"\n[IPFS] {resp.status} {url}", flush=True)
        except Exception as e:
            print(f"\n[IPFS] error fetching {url}: {e}", flush=True)
        pbar.update(1)
        return None


async def fetch_all_ipfs(cids, gateway, timeout, concurrency, desc="Downloading IPFS", headers=None):
    if not cids: return {}
    sem = asyncio.Semaphore(concurrency)
    pbar = tqdm(total=len(cids), desc=f"  ↳ {desc}", unit=" file")
    async with aiohttp.ClientSession(headers=headers or {}) as session:
        tasks = [
            asyncio.create_task(_fetch_json(session, sem, f"{gateway.rstrip('/')}/{_cid_to_path(cid)}", timeout, pbar))
            for cid in cids
        ]
        results = await asyncio.gather(*tasks)
    pbar.close()
    return dict(zip(cids, results))
