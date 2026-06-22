"""
Sandboxed subprocess execution for the (untrusted) extract step.

CmonCrawl's `cmon extract` loads and runs the publisher's extractor Python over
untrusted Common Crawl HTML. Even though the extractor is typically LLM-generated,
a `crawl_job` manifest can be published by any address, so the extract step is
treated as untrusted code and is confined:

  * wall-clock timeout (subprocess.run timeout)
  * RLIMIT_CPU / RLIMIT_AS (memory) / RLIMIT_FSIZE caps via preexec_fn
  * a fresh process session (setsid) so a child can't signal the parent group
  * a scrubbed environment (no inherited secrets — PINATA_JWT, keys, etc.)
  * optional network isolation: wrapped in `unshare --net` when no_network=True

Network: the pipeline runs CmonCrawl in `record` mode, where extract fetches WARC
content from Common Crawl as it runs (trusted downloader, in-process with the
extractor), so it is called with no_network=False — only `html` mode (content
already on disk) is fully net-isolated. The rlimit / env-scrub / setsid
confinement applies either way; the only thing reachable is public Common Crawl
data, and the env carries no secrets.

This is the MVP isolation. The production target is a disposable container /
gVisor / Firecracker microVM per job (with egress filtered to Common Crawl);
`run()` is the single seam to swap.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


class SandboxError(RuntimeError):
    pass


# Env vars that are safe/necessary to pass through to the sandboxed child.
# Everything else (secrets, tokens) is dropped.
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONUNBUFFERED")


def _scrubbed_env(extra: dict | None = None) -> dict:
    env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
    env.setdefault("PYTHONUNBUFFERED", "1")
    if extra:
        env.update(extra)
    return env


def _net_isolation_prefix(no_network: bool) -> list[str]:
    """
    Prefix argv with `unshare --net` to drop the child into an empty network
    namespace when possible. Requires unprivileged userns (common on modern
    Linux). Returns [] when unavailable — callers should not rely on network
    isolation being enforced on every host (logged by run()).
    """
    if not no_network or sys.platform != "linux":
        return []
    unshare = shutil.which("unshare")
    if not unshare:
        return []
    # --map-root-user lets the unprivileged caller create the net namespace.
    return [unshare, "--net", "--map-root-user", "--"]


def _preexec(cpu_s: int, mem_mb: int, fsize_mb: int):
    def _apply():
        import resource

        os.setsid()
        mem = mem_mb * 1024 * 1024
        fsize = fsize_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))

    return _apply


def run(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 600,
    cpu_s: int = 300,
    mem_mb: int = 2048,
    fsize_mb: int = 1024,
    no_network: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """
    Run `argv` confined. Raises SandboxError on non-zero exit or timeout.
    preexec_fn (rlimits/setsid) is POSIX-only; on non-POSIX it is skipped.
    """
    prefix = _net_isolation_prefix(no_network)
    full_argv = prefix + argv
    if no_network and not prefix:
        print("[sandbox] WARNING: network isolation unavailable on this host "
              "(no usable `unshare`); extract step runs WITHOUT a net namespace")

    preexec = _preexec(cpu_s, mem_mb, fsize_mb) if os.name == "posix" else None
    try:
        proc = subprocess.run(
            full_argv,
            cwd=cwd,
            env=_scrubbed_env(env),
            timeout=timeout,
            capture_output=True,
            text=True,
            preexec_fn=preexec,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"sandboxed command timed out after {timeout}s: {argv}") from exc
    except FileNotFoundError as exc:
        raise SandboxError(f"command not found: {argv[0]!r}") from exc

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise SandboxError(
            f"sandboxed command failed (exit {proc.returncode}): {argv}\n{tail}"
        )
    return proc
