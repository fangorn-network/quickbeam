"""Owner:namespace source bridge — the read side of the new data model.

Quickbeam reads a namespace's graph directly off-chain via the `fangorn` CLI (a
light client — no subgraph, no IPFS gateway, no bundle schema):

  • `fangorn read <ns> --owner <addr>`      → the full `{vertices, edges}` snapshot
  • `fangorn subscribe <ns> --owner <addr>` → a stream of on-chain diffs (the live tail)

The Fangorn SDK's read primitives are TypeScript-only, so — exactly as the publish
leg already does — we shell out to the CLI. `--fangorn-bin` may be a FULL command
(shell-split), not just a path, e.g. the git-native dev wrapper
`dotenvx run -f ~/fangorn/fangorn/.env -- node ~/fangorn/fangorn/lib/cli/cli.js`.
"""
from __future__ import annotations

import json
import shlex
import subprocess


def parse_sources(raw_sources: list[str]) -> list[tuple[str, str]]:
    """Parse --source OWNER:NAMESPACE pairs."""
    out = []
    for s in raw_sources:
        owner, sep, namespace = s.partition(":")
        if not sep or not owner.strip() or not namespace.strip():
            raise SystemExit(f"Invalid --source {s!r}, expected OWNER:NAMESPACE")
        out.append((owner.strip(), namespace.strip()))
    return out


def read_source(fangorn_bin: str, owner: str, namespace: str) -> dict:
    """Shell out to `fangorn read <namespace> --owner <owner>` and parse the JSON
    {owner, namespace, head, vertices, edges} it prints to stdout."""
    prefix = shlex.split(fangorn_bin)
    cmd = [*prefix, "read", namespace, "--owner", owner]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            f"fangorn CLI not found (--fangorn-bin {fangorn_bin!r}, resolved to "
            f"{prefix[0]!r}). Install it or pass its full invocation, e.g. "
            f"--fangorn-bin \"dotenvx run -f ~/fangorn/fangorn/.env -- node "
            f"~/fangorn/fangorn/lib/cli/cli.js\".")
    if result.returncode != 0:
        raise RuntimeError(f"fangorn read {owner}:{namespace} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def read_head(fangorn_bin: str, owner: str) -> str:
    """Shell out to `fangorn head <owner>` — the cheap on-chain root check (used to
    skip a cycle with no on-chain change)."""
    prefix = shlex.split(fangorn_bin)
    cmd = [*prefix, "head", owner]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"fangorn CLI not found (--fangorn-bin {fangorn_bin!r})")
    if result.returncode != 0:
        raise RuntimeError(f"fangorn head {owner} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def subscribe_cmd(fangorn_bin: str, owner: str, namespace: str) -> list[str]:
    """Argv for `fangorn subscribe <namespace> --owner <owner>` — a light-client
    stream that emits one `NamespaceChange` JSON per line on stdout as commits land
    (status/logs go to stderr; the CLI persists its own resume cursor under
    ./.fangorn). Each line is a self-contained on-chain diff:
        {namespace, owner, commitCid, oldRoot, newRoot, blockNumber,
         addedVertices:[{cid,schemaId,payload}], addedEdges:[{sourceCid,relation,targetCid}],
         removedVertexCids:[cid], removedEdges:[...]}
    This is the push-based replacement for `read_head` polling: the chain tells us
    exactly what changed instead of us re-reading the whole namespace on a timer."""
    return [*shlex.split(fangorn_bin), "subscribe", namespace, "--owner", owner]
