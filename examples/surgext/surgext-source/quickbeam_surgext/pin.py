"""
Pin the extracted manual figures to IPFS (Pinata) so they travel with the on-chain
graph. Each image becomes a content-addressed IPFS blob; its CID is written to
`<image_dir>/image-cids.json` ({file: cid}), which `extract` then folds into each
section's `fields.images[].cid`. A Fangorn consumer reading the published graph
resolves the image from any gateway (`<gateway>/ipfs/<cid>`) — the standard IPLD
"reference big blobs by CID" pattern, on the same Pinata storage the SDK uses.

This is the ONE on-chain step you run yourself (it needs your Pinata JWT); it's
independent of the off-chain website, which serves figures from its private bundle.

    surgext-pin-images --image-dir ./examples/surgext/manual-build/images --pinata-jwt "$PINATA_JWT"

Idempotent: CIDs are content-derived, so already-pinned files (present in the map)
are skipped, and re-running never re-uploads unchanged images.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

_PIN_URL = "https://api.pinata.cloud/pinning/pinFileToIPFS"


def _pin_one(path: str, name: str, jwt: str, retries: int = 3) -> str:
    """Pin one file and return its CID. A fresh `requests.post` per call rather than a
    shared `requests.Session` — Session isn't thread-safe. Retries 429/5xx: Pinata's
    upload endpoint 408s/5xxs intermittently under exactly the parallel load we create."""
    for attempt in range(retries):
        with open(path, "rb") as fh:
            resp = requests.post(
                _PIN_URL,
                headers={"Authorization": f"Bearer {jwt}"},
                files={"file": (name, fh, "image/png")},
                data={"pinataOptions": json.dumps({"cidVersion": 1})},
                timeout=120,
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == retries - 1:
                resp.raise_for_status()
            time.sleep(2**attempt)
            continue
        resp.raise_for_status()
        return resp.json()["IpfsHash"]
    raise RuntimeError(f"unreachable: {name}")  # pragma: no cover


def pin_images(image_dir: str, jwt: str, *, verbose: bool = True,
               concurrency: int = 8) -> dict[str, str]:
    """Pin every `*.png` in `image_dir` to Pinata (CIDv1) and return/persist {file: cid}.

    Latency-bound (the files are ~84 KB), so uploads run in parallel. The map is persisted
    even if a worker raises, so a failed run keeps the CIDs it already earned and a re-run
    resumes from there via the skip-if-present check."""
    cids_path = os.path.join(image_dir, "image-cids.json")
    cids: dict[str, str] = {}
    if os.path.exists(cids_path):
        try:
            cids = json.load(open(cids_path))
        except (json.JSONDecodeError, OSError):
            cids = {}

    files = [f for f in sorted(os.listdir(image_dir)) if f.endswith(".png")]
    todo = [f for f in files if f not in cids]  # content-addressed → stable, never re-pin

    try:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            futs = {ex.submit(_pin_one, os.path.join(image_dir, f), f, jwt): f
                    for f in todo}
            done = 0
            for fut in as_completed(futs):
                cids[futs[fut]] = fut.result()
                done += 1
                if verbose:
                    print(f"[pin] pinned {done}/{len(todo)}: {futs[fut]}")
    finally:
        with open(cids_path, "w") as fh:
            json.dump(cids, fh, indent=2, sort_keys=True)

    if verbose:
        print(f"[pin] {len(cids)} image(s) pinned ({len(todo)} new) -> {cids_path}")
    return cids


def main() -> None:
    p = argparse.ArgumentParser(
        prog="surgext-pin-images",
        description="Pin the manual's figures to IPFS (Pinata) and write image-cids.json "
                    "for the on-chain publish.")
    p.add_argument("--image-dir", required=True,
                   help="The figures directory produced by `quickbeam data surgext --image-dir`.")
    p.add_argument("--pinata-jwt", default=os.environ.get("PINATA_JWT"),
                   help="Your Pinata JWT (or set PINATA_JWT). Same account the Fangorn SDK uses.")
    p.add_argument("--concurrency", type=int, default=8, help="Parallel uploads.")
    args = p.parse_args()
    if not args.pinata_jwt:
        sys.exit("[pin] a Pinata JWT is required (--pinata-jwt or PINATA_JWT env).")
    pin_images(args.image_dir, args.pinata_jwt, concurrency=args.concurrency)


if __name__ == "__main__":
    main()
