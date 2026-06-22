"""
Crawl pipeline orchestration: CrawlJob -> Fangorn `{name, fields}` records.

Phases:
  1. materialize  — write extractor modules + config.json (materialize.py)
  2. download     — `cmon download` HTML for the query (trusted, networked)
  3. extract      — `cmon extract` under the sandbox (untrusted extractor code)
  4. transform    — read extract output -> {name, fields} (transform.py)

Pure orchestration: no Fangorn/subgraph coupling, so it's reusable from both the
scraper service and the offline `quickbeam data crawl` CLI, and unit-testable by
injecting `download_fn` / `extract_runner`.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from quickbeam.crawl import cmon, materialize, sandbox, transform
from quickbeam.crawl.config import CrawlJob


@dataclass
class SandboxLimits:
    timeout: int = 600
    cpu_s: int = 300
    mem_mb: int = 2048
    fsize_mb: int = 1024
    no_network: bool = True


def run_crawl(
    job: CrawlJob,
    *,
    workdir: str | Path | None = None,
    cmon_bin: str | None = None,
    mode: str = "record",
    limits: SandboxLimits | None = None,
    download_timeout: int = 1800,
    n_proc: int = 1,
    # Injection seams for tests / alternate backends.
    download_fn: Callable | None = None,
    extract_runner: Callable[[list[str]], object] | None = None,
) -> list[dict]:
    """
    Execute the full crawl for `job` and return Fangorn records.

    `job.extractor_sources` must already be populated (the scraper service
    resolves any `extractorRefs` from IPFS before calling this).
    """
    limits = limits or SandboxLimits()
    owns_workdir = workdir is None
    workdir = Path(workdir or tempfile.mkdtemp(prefix="quickbeam-crawl-"))

    try:
        config_path = materialize.materialize(job.routes, job.extractor_sources, workdir)

        html_dir = workdir / "downloaded"
        if download_fn is not None:
            files = download_fn(job.query, html_dir, mode=mode, cmon_bin=cmon_bin)
        else:
            files = cmon.download(job.query, html_dir, mode=mode,
                                  cmon_bin=cmon_bin, timeout=download_timeout)
        if not files:
            print("[crawl] download produced no files — empty result")
            return []
        print(f"[crawl] downloaded {len(files)} {mode} file(s); extracting…")

        # Default the extract runner to the sandbox unless a test injects one.
        # `record` mode fetches WARC content from Common Crawl *during* extract
        # (CmonCrawl's trusted AsyncDownloader, in-process with the extractor), so
        # a network namespace can't be used there — only `html` mode (content
        # already on disk) can be fully network-isolated. Other confinement
        # (rlimits, scrubbed env, scratch fs) still applies in both modes.
        runner = extract_runner
        extract_no_network = limits.no_network and mode == "html"
        if runner is None:
            def runner(argv):  # noqa: E306 — small local closure
                return sandbox.run(
                    argv, cwd=str(workdir),
                    timeout=limits.timeout, cpu_s=limits.cpu_s,
                    mem_mb=limits.mem_mb, fsize_mb=limits.fsize_mb,
                    no_network=extract_no_network,
                )

        extract_dir = workdir / "extracted"
        # In `record` mode extract fetches WARC content from Common Crawl as it
        # runs, so it is network-bound — `n_proc > 1` lets CmonCrawl process the
        # per-URL record files in parallel, which is what keeps a wide multi-domain
        # crawl inside the wall-clock timeout.
        cmon.extract(config_path, extract_dir, [str(f) for f in files],
                     mode=mode, n_proc=n_proc, cmon_bin=cmon_bin, runner=runner)

        records = transform.to_records(extract_dir, name_seed=job.output_schema.split(".")[-1] or "page")
        print(f"[crawl] extracted {len(records)} record(s)")
        return records
    finally:
        if owns_workdir:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
