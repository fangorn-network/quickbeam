"""
Python → Fangorn publish bridge.

Shells out to `node src/publish.mjs` (which wraps @fangorn-network/sdk) to register
a schema and publish `{name, fields}` records as a Fangorn dataset. We use the
canonical JS SDK rather than reimplementing manifest building / Merkle commitment
in Python.

Requires `node` on PATH, the SDK installed (`npm install`), and the env the script
needs: FANGORN_PRIVATE_KEY, PINATA_JWT (+ optional PINATA_GATEWAY, RPC_URL).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

_RESULT_MARKER = "__FANGORN_RESULT__"

# Repo root → src/publish.mjs, resolved relative to this file so it works whether
# the service is launched from the repo root or elsewhere.
_DEFAULT_SCRIPT = str(Path(__file__).resolve().parent.parent / "src" / "publish.mjs")


class PublishError(RuntimeError):
    pass


def publish_records(
    records: list[dict],
    *,
    schema_name: str,
    dataset_name: str | None = None,
    schema_def: dict | None = None,
    chunk_size: int = 1000,
    node_bin: str = "node",
    publish_script: str | None = None,
    env: dict | None = None,
    timeout: int = 900,
) -> dict:
    """
    Publish `records` under `schema_name`. If `schema_def` is given the schema is
    registered first (idempotent). Returns the parsed result dict, e.g.
    {"manifestUri": "...", "schemaId": "0x...", "dataset": "...", "entryCount": N}.
    """
    if not records:
        raise PublishError("no records to publish")
    script = publish_script or _DEFAULT_SCRIPT
    if not Path(script).exists():
        raise PublishError(f"publish script not found: {script}")

    run_env = {**os.environ, **(env or {})}
    for required in ("FANGORN_PRIVATE_KEY", "PINATA_JWT"):
        if not run_env.get(required):
            raise PublishError(f"{required} must be set to publish to Fangorn")

    with tempfile.TemporaryDirectory(prefix="quickbeam-publish-") as tmp:
        recs_path = Path(tmp) / "records.jsonl"
        with recs_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        argv = [node_bin, script, "--records", str(recs_path),
                "--schema", schema_name, "--chunk-size", str(chunk_size)]
        if dataset_name:
            argv += ["--dataset", dataset_name]
        if schema_def is not None:
            def_path = Path(tmp) / "schema.json"
            def_path.write_text(json.dumps(schema_def), encoding="utf-8")
            argv += ["--schema-def", str(def_path)]

        proc = subprocess.run(argv, capture_output=True, text=True, env=run_env, timeout=timeout)
        if proc.returncode != 0:
            raise PublishError(
                f"publish.mjs failed (exit {proc.returncode}):\n"
                f"{(proc.stderr or proc.stdout or '')[-3000:]}"
            )

    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_RESULT_MARKER):
            return json.loads(line[len(_RESULT_MARKER):].strip())
    raise PublishError(f"publish.mjs produced no {_RESULT_MARKER} line:\n{proc.stdout[-2000:]}")
