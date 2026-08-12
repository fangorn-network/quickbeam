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


def app_args(app: str | None) -> list[str]:
    """`--app <name-or-id>` for the fangorn CLI, or nothing. The app id prefixes every
    namespace key, so it decides which global namespace a read/subscribe sees. Unset means
    whatever the local client is configured with (`fangorn set-app`) — which is why a
    daemon watching someone else's app should always pass it explicitly."""
    return ["--app", app] if app else []


def parse_sources(raw_sources: list[str]) -> list[tuple[str | None, str | None]]:
    """Parse --source OWNER:NAMESPACE pairs. `*` on either side is a wildcard, which
    widens the subscription to the app-level topic filter (the app id is whatever the
    `app` argument, else whatever the local fangorn client is configured with):

        0x7a78...:sond3r.test.1  one publisher's one subspace (the tightest filter)
        0x7a78...:*             one publisher, every subspace in the app
        *:sond3r.test.1         that subspace name across every publisher
        *:*                     the whole app

    A wildcard side comes back as None."""
    out = []
    for s in raw_sources:
        owner, sep, namespace = s.partition(":")
        if not sep or not owner.strip() or not namespace.strip():
            raise SystemExit(f"Invalid --source {s!r}, expected OWNER:NAMESPACE (`*` = any)")
        out.append((None if owner.strip() == "*" else owner.strip(),
                    None if namespace.strip() == "*" else namespace.strip()))
    return out


def read_source(fangorn_bin: str, owner: str, namespace: str, app: str | None = None) -> dict:
    """Shell out to `fangorn read <namespace> --owner <owner>` and parse the JSON
    {owner, namespace, head, vertices, edges} it prints to stdout."""
    prefix = shlex.split(fangorn_bin)
    cmd = [*prefix, *app_args(app), "read", namespace, "--owner", owner]
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


def subscribe_cmd(fangorn_bin: str, owner: str | None, namespace: str | None,
                  from_start: bool = False, from_block: int | None = None,
                  app: str | None = None) -> list[str]:
    """Argv for `fangorn subscribe <namespace> --owner <owner>` — a light-client
    stream that emits one `NamespaceChange` JSON per line on stdout as commits land
    (status/logs go to stderr; the CLI persists its own resume cursor under
    ./.fangorn). Each line is a self-contained on-chain diff:
        {namespace, owner, commitCid, oldRoot, newRoot, blockNumber,
         addedVertices:[{cid,schemaId,payload}], addedEdges:[{sourceCid,relation,targetCid}],
         removedVertexCids:[cid], removedEdges:[...]}
    This is the push-based replacement for `read_head` polling: the chain tells us
    exactly what changed instead of us re-reading the whole namespace on a timer.

    A None owner or namespace (a `*` wildcard source) switches the CLI to `--all`: the
    filter widens to the app id and the unset side matches every publisher/subspace. `app`
    picks which app that is (see app_args); unset uses the local client's configured one.

    `from_block` (or `from_start`, its genesis form) replays history before going live —
    how an app-level watch discovers namespaces published before it started, since a
    wildcard source can't be seeded with `read` (which needs one exact namespace). Prefer
    `from_block`: the catch-up is windowed, and genesis on a fast chain like Arbitrum is
    hundreds of thousands of windows."""
    argv = [*shlex.split(fangorn_bin), *app_args(app), "subscribe"]
    if owner is None or namespace is None:
        argv.append("--all")
    if from_block is not None:
        argv += ["--from-block", str(from_block)]
    elif from_start:
        argv.append("--from-start")
    if namespace is not None:
        argv.append(namespace)
    if owner is not None:
        argv += ["--owner", owner]
    return argv
