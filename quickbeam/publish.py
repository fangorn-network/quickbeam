"""
`Publisher` — the programmatic SDK façade for publishing a `Source` to Fangorn.

Everything a data publisher needs in a few lines of Python, without touching the CLI
or hand-editing entry points:

    import quickbeam as qb
    from my_scraper import MySource             # YOUR Source — quickbeam ships none

    pub = qb.Publisher(MySource(), repo="./my-data",
                       prefix="me.mysrc", bundle_name="widgets")
    pub.run(api_url="https://example.com/api")  # onboard (first time) → ingest → publish

`Publisher` is a THIN composition over machinery that already exists — it invents no new
pipeline logic:

  • ingest   → `quickbeam.ingest.scrapers.harness.ingest_once` (read → emit staged volumes
               → checkpoint), driven through the same argparse `Namespace` the CLI builds.
  • onboard  → `quickbeam.pipelines.fangorn_schema.generate_schemas` (infer node schemas +
               a bundle shape) + `fangorn repo init` against the inferred bundle schema.
  • publish  → `fangorn commit --bundle` (auto-registers missing schemas) + `fangorn push`.

One Fangorn repo carries ONE bundle schema, so publish OSM and Robinhood to SEPARATE
repos (two `Publisher`s with distinct `repo=` dirs).
"""
from __future__ import annotations

import datetime
import os

from .ingest.scrapers import (Source, build_parser, fangorn_commit_push,
                              fangorn_repo_init, ingest_once)
from .pipelines.fangorn_schema import generate_schemas

__all__ = ["Publisher"]


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class Publisher:
    """Bind a `Source` to a Fangorn repo and drive the onboard → ingest → publish loop.

    Args:
        source:       any object satisfying the `Source` contract (built-in or your own).
        repo:         directory holding the Fangorn repo (`.fangorn/`). Created by onboard.
        output_dir:   where staged `volume_<n>_*.json` files are written
                      (default `<repo>/stage_volumes`).
        prefix:       schema-name prefix → `<prefix>.<type>.<version>`.
        bundle_name:  bundle stem → `<prefix>.<bundle_name>.<version>` (the repo's schema).
        version:      schema version tag (default "v1").
        volume:       volume number (default `source.default_volume` or 1).
        fangorn_bin:  the fangorn CLI invocation — a full command, shell-split
                      (default "fangorn"; pass the dev wrapper for the git-native build).
    """

    def __init__(self, source: Source, *, repo: str = ".",
                 prefix: str, bundle_name: str, version: str = "v1",
                 output_dir: str | None = None, volume: int | None = None,
                 fangorn_bin: str = "fangorn") -> None:
        self.source = source
        self.repo = repo
        self.prefix = prefix
        self.bundle_name = bundle_name
        self.version = version
        self.output_dir = output_dir or os.path.join(repo, "stage_volumes")
        self.volume = volume if volume is not None else getattr(source, "default_volume", 1)
        self.fangorn_bin = fangorn_bin
        # Set once schemagen runs — the full bundle schema name `repo init` points at.
        self.bundle_schema_name: str | None = None

    # ── argparse bridge ──────────────────────────────────────────────────────────
    def _build_args(self, source_kwargs: dict):
        """Build the argparse `Namespace` `ingest_once` expects: start from the shared +
        source defaults (`build_parser(source).parse_args([])`), then override with the
        Publisher config and the caller's `source_kwargs`. Kwarg keys are argparse dests
        (underscored): `place=`, `with_transfers=`, `max_transfers=`, `accumulate=`,
        `checkpoint_file=`, `dry_run=` … Unknown keys raise so typos aren't swallowed."""
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
        args.repo = self.repo
        args.fangorn_bin = self.fangorn_bin
        args.publish = False          # publishing is a separate, explicit step here
        for k, v in source_kwargs.items():
            setattr(args, k, v)
        return args

    def _staged_volumes(self) -> list[str]:
        import glob
        return sorted(glob.glob(
            os.path.join(self.output_dir, f"volume_{self.volume}_*.json")))

    # ── the three legs ───────────────────────────────────────────────────────────
    def ingest(self, **source_kwargs) -> "Publisher":
        """Read the source and stage `volume_<n>_*.json` node/edge files (→ checkpoint).
        Pass source flags as kwargs (see `_build_args`). `dry_run=True` previews without
        writing. Returns self for chaining."""
        ingest_once(self.source, self._build_args(source_kwargs))
        return self

    def onboard(self, *, force: bool = False, **ingest_kwargs) -> "Publisher":
        """Bootstrap the Fangorn repo: infer schemas from the staged volumes and
        `fangorn repo init` against the inferred bundle schema. If nothing is staged yet
        and `ingest_kwargs` are given, ingest first; otherwise raise (schemagen needs
        sample data). Idempotent: skips `repo init` when `<repo>/.fangorn` already exists
        unless `force=True`. Schema REGISTRATION happens automatically on the first
        `publish()` (commit --bundle registers missing schemas)."""
        if not self._staged_volumes():
            if ingest_kwargs:
                self.ingest(**ingest_kwargs)
            else:
                raise RuntimeError(
                    f"onboard: no staged volumes in {self.output_dir!r} for volume "
                    f"{self.volume}. Call ingest(...) first, or pass ingest kwargs to "
                    f"onboard(...) so it can sample the source for schema inference.")
        result = generate_schemas(
            input_dir=self.output_dir, volume=self.volume,
            prefix=self.prefix, version=self.version, bundle_name=self.bundle_name)
        self.bundle_schema_name = result["bundle_name"]

        if force or not os.path.isdir(os.path.join(self.repo, ".fangorn")):
            os.makedirs(self.repo, exist_ok=True)
            name = os.path.basename(os.path.abspath(self.repo))
            ok = fangorn_repo_init(
                repo=self.repo, fangorn_bin=self.fangorn_bin,
                name=name, schema_name=self.bundle_schema_name, tag=self.source.name)
            if not ok:
                raise RuntimeError(
                    f"onboard: `fangorn repo init {name} -s {self.bundle_schema_name}` "
                    f"failed in {self.repo!r}. Run `fangorn init` to configure credentials "
                    f"first, then retry.")
        else:
            print(f"[{self.source.name}] repo already initialized "
                  f"({os.path.join(self.repo, '.fangorn')}); skipping repo init")
        return self

    def publish(self, *, message: str | None = None) -> "Publisher":
        """Commit the staged bundle on-chain (`fangorn commit --bundle`, auto-registering
        any missing schemas from `<output_dir>/schemas`) and `fangorn push`. Requires an
        onboarded repo (`<repo>/.fangorn`). Returns self for chaining."""
        if not os.path.isdir(os.path.join(self.repo, ".fangorn")):
            raise RuntimeError(
                f"publish: {self.repo!r} is not a Fangorn repo (no .fangorn/). Call "
                f"onboard(...) first to infer schemas and `fangorn repo init`.")
        msg = message or f"{self.source.name} snapshot {_utc_now_iso()}"
        ok = fangorn_commit_push(
            repo=self.repo, fangorn_bin=self.fangorn_bin, output_dir=self.output_dir,
            volume=self.volume, message=msg,
            schemas_dir=os.path.join(self.output_dir, "schemas"), tag=self.source.name)
        if not ok:
            raise RuntimeError(f"publish: fangorn commit/push failed (see log above).")
        return self

    def run(self, *, message: str | None = None, **source_kwargs) -> "Publisher":
        """The one-call path: ingest → onboard (first time; schemagen + repo init) →
        publish. Safe to re-run — onboard skips `repo init` once the repo exists, and
        each publish is a fresh snapshot/ledger commit."""
        self.ingest(**source_kwargs)
        if not os.path.isdir(os.path.join(self.repo, ".fangorn")):
            self.onboard()
        else:
            # Repo exists but schemas may need refreshing (fields can grow between runs);
            # regenerate so commit --bundle always sees a current schemas dir.
            self.onboard(force=False)
        return self.publish(message=message)
