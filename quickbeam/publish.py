"""
`Publisher` — the programmatic SDK façade for publishing a `Source` to Fangorn.

Everything a data publisher needs in a few lines of Python, without touching the CLI:

    import quickbeam as qb
    from my_scraper import MySource             # YOUR Source — quickbeam ships none

    pub = qb.Publisher(MySource(), namespace="widgets")
    pub.run()                                   # ingest → publish (repo init + commit + push)

`Publisher` is a THIN composition over machinery that already exists — it invents no new
pipeline logic:

  • ingest   → `quickbeam.ingest.scrapers.harness.ingest_once` (read → emit staged volumes
               → checkpoint), driven through the same argparse `Namespace` the CLI builds.
  • publish  → `fangorn repo init <namespace>` (idempotent) + `fangorn commit -m <msg>` +
               `fangorn push`, assembling the staged volumes into one on-chain commit.

Publishing is owner:namespace: the push settles into the configured wallet's namespace,
which `quickbeam watch --source <owner>:<namespace>` reads back. Point separate scrapers
at separate namespaces (two `Publisher`s with distinct `namespace=` values).
"""
from __future__ import annotations

import os

from .ingest.scrapers import Source, build_parser, ingest_once
from .ingest.scrapers.harness import _publish_to_fangorn

__all__ = ["Publisher"]


class Publisher:
    """Bind a `Source` to a Fangorn namespace and drive the ingest → publish loop.

    Args:
        source:       any object satisfying the `Source` contract (built-in or your own).
        namespace:    the Fangorn namespace to publish into (under the configured wallet).
        output_dir:   where staged `volume_<n>_*.json` files are written
                      (default `./stage_volumes`).
        volume:       volume number (default `source.default_volume` or 1).
        fangorn_bin:  the fangorn CLI invocation — a full command, shell-split
                      (default "fangorn", a global install; pass a wrapper if you run
                      fangorn via dotenvx/node instead).
    """

    def __init__(self, source: Source, *, namespace: str,
                 output_dir: str | None = None, volume: int | None = None,
                 fangorn_bin: str = "fangorn") -> None:
        self.source = source
        self.namespace = namespace
        self.output_dir = output_dir or "./stage_volumes"
        self.volume = volume if volume is not None else getattr(source, "default_volume", 1)
        self.fangorn_bin = fangorn_bin

    # ── argparse bridge ──────────────────────────────────────────────────────────
    def _build_args(self, source_kwargs: dict):
        """Build the argparse `Namespace` `ingest_once`/`_publish_to_fangorn` expect:
        start from the shared + source defaults (`build_parser(source).parse_args([])`),
        then override with the Publisher config and the caller's `source_kwargs`. Kwarg
        keys are argparse dests (underscored): `with_transfers=`, `max_transfers=`,
        `accumulate=`, `checkpoint_file=`, `dry_run=` … Unknown keys raise so typos
        aren't swallowed."""
        args = build_parser(self.source).parse_args([])
        valid = set(vars(args))
        unknown = set(source_kwargs) - valid
        if unknown:
            raise TypeError(
                f"unknown ingest argument(s) {sorted(unknown)} for source "
                f"{self.source.name!r}; valid options: {sorted(valid)}")
        # Publisher owns these; the caller configures the rest via source_kwargs.
        args.output_dir = self.output_dir
        args.volume = self.volume
        args.namespace = self.namespace
        args.fangorn_bin = self.fangorn_bin
        args.publish = False          # publishing is a separate, explicit step here
        for k, v in source_kwargs.items():
            setattr(args, k, v)
        return args

    def _staged_volumes(self) -> list[str]:
        import glob
        return sorted(glob.glob(
            os.path.join(self.output_dir, f"volume_{self.volume}_*.json")))

    # ── the two legs ─────────────────────────────────────────────────────────────
    def ingest(self, **source_kwargs) -> "Publisher":
        """Read the source and stage `volume_<n>_*.json` node/edge files (→ checkpoint).
        Pass source flags as kwargs (see `_build_args`). `dry_run=True` previews without
        writing. Returns self for chaining."""
        ingest_once(self.source, self._build_args(source_kwargs))
        return self

    def publish(self, **source_kwargs) -> "Publisher":
        """Publish the staged volumes into `self.namespace`: assemble them into one
        `{vertices, edges}` batch and run `fangorn repo init <namespace>` (idempotent) +
        `fangorn commit -m <msg>` + `fangorn push`. Raises if nothing is staged or the
        publish fails. Returns self for chaining."""
        if not self._staged_volumes():
            raise RuntimeError(
                f"publish: no staged volumes in {self.output_dir!r} for volume "
                f"{self.volume}. Call ingest(...) first.")
        if not _publish_to_fangorn(self.source, self._build_args(source_kwargs)):
            raise RuntimeError("publish: fangorn repo init/commit/push failed (see log above).")
        return self

    def run(self, **source_kwargs) -> "Publisher":
        """The one-call path: ingest → publish. Safe to re-run — `repo init` is
        idempotent and each cycle is a fresh commit on top of the on-chain tip (fangorn's
        structural sharing re-uploads only what changed)."""
        self.ingest(**source_kwargs)
        return self.publish()
