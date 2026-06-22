"""
Materialize a CmonCrawl extract config + extractor modules onto disk.

CmonCrawl's `cmon extract` takes a `config.json` of the form:

    {"extractors_path": "<dir>", "routes": [ ... ]}

where each route references an extractor by `name`, and `<dir>/<name>.py` is a
Python module exposing an `extractor` variable. We write the publisher-provided
sources there verbatim and emit the config pointing at them.
"""

from __future__ import annotations

import json
from pathlib import Path


def materialize(routes: list[dict], extractor_sources: dict[str, str], workdir: str | Path) -> Path:
    """
    Write extractor modules + config.json under `workdir`. Returns the config path.

    `extractor_sources` maps extractor name -> Python source. Every name the
    routes reference must be present, else extraction would fail inside the
    sandbox with a less obvious error — so we validate up front.
    """
    workdir = Path(workdir)
    ex_dir = workdir / "extractors"
    ex_dir.mkdir(parents=True, exist_ok=True)

    referenced: set[str] = set()
    for route in routes:
        for ex in route.get("extractors", []) or []:
            if isinstance(ex, dict) and ex.get("name"):
                referenced.add(str(ex["name"]))

    missing = sorted(referenced - set(extractor_sources))
    if missing:
        raise ValueError(f"routes reference extractors with no source: {missing}")

    for name, source in extractor_sources.items():
        # Defend against path traversal in attacker-controlled names.
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"illegal extractor name: {name!r}")
        (ex_dir / f"{name}.py").write_text(source, encoding="utf-8")

    config = {"extractors_path": str(ex_dir), "routes": routes}
    config_path = workdir / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path
