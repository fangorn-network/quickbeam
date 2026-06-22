"""
Offline unit tests for the crawl pipeline core — no network, no `cmon`, no chain.

Covers job parsing + payment key stability, extractor materialization (incl.
path-traversal rejection), extract-output transformation, the sandbox runner, and
the full run_crawl orchestration with download + extract injected.
"""

import json
from pathlib import Path

import pytest

from quickbeam.crawl import sandbox, transform
from quickbeam.crawl.config import CrawlJob, payment_key_for_fields
from quickbeam.crawl.materialize import materialize
from quickbeam.crawl.pipeline import run_crawl


# ── config / payment key ─────────────────────────────────────────────────────
def _job_fields(**over):
    base = {
        "routes": [{"regexes": [".*"], "extractors": [{"name": "ex1"}]}],
        "extractors": {"ex1": "extractor = object()\n"},
        "query": {"urls": ["example.com"], "matchType": "domain", "limit": 5},
        "outputSchema": "fangorn.webpage.v1",
    }
    base.update(over)
    return base


def test_job_parse_and_extractor_names():
    job = CrawlJob.from_fields(_job_fields())
    assert job.output_schema == "fangorn.webpage.v1"
    assert job.extractor_names() == {"ex1"}
    assert job.query.urls == ["example.com"]
    assert job.query.match_type == "domain"


def test_payment_key_ignores_inline_source_but_tracks_billable_content():
    a = payment_key_for_fields(_job_fields())
    # changing only the extractor source must NOT change the bill
    b = payment_key_for_fields(_job_fields(extractors={"ex1": "extractor = 1  # different\n"}))
    assert a == b
    # changing the query DOES change the bill
    c = payment_key_for_fields(_job_fields(query={"urls": ["other.com"], "matchType": "domain"}))
    assert a != c


def test_extractors_typed_array_form():
    # Canonical on-chain form: extractors is an array of {name, source|sourceCid}.
    job = CrawlJob.from_fields({
        "routes": [{"regexes": [".*"], "extractors": [{"name": "a"}, {"name": "b"}]}],
        "extractors": [
            {"name": "a", "language": "python", "source": "extractor = 1\n", "sourceCid": None},
            {"name": "b", "source": None, "sourceCid": "bafyB"},
        ],
        "query": {"urls": ["x.com"], "matchType": "domain", "limit": 5},
        "outputSchema": "s",
    })
    assert job.extractor_sources == {"a": "extractor = 1\n"}
    assert job.extractor_refs == {"b": "bafyB"}
    assert job.extractor_names() == {"a", "b"}


def test_extractors_map_form_still_supported():
    job = CrawlJob.from_fields(_job_fields())  # uses {name: source} map
    assert job.extractor_sources == {"ex1": "extractor = object()\n"}


def test_job_requires_routes_and_schema():
    with pytest.raises(ValueError):
        CrawlJob.from_fields({"query": {"urls": ["x.com"]}, "outputSchema": "s"})
    with pytest.raises(ValueError):
        CrawlJob.from_fields({"routes": [{"extractors": []}], "query": {"urls": ["x.com"]}})


# ── materialize ───────────────────────────────────────────────────────────────
def test_materialize_writes_modules_and_config(tmp_path):
    routes = [{"regexes": [".*"], "extractors": [{"name": "ex1"}]}]
    cfg = materialize(routes, {"ex1": "extractor = 1\n"}, tmp_path)
    data = json.loads(Path(cfg).read_text())
    assert data["routes"] == routes
    assert (Path(data["extractors_path"]) / "ex1.py").read_text() == "extractor = 1\n"


def test_materialize_rejects_missing_source(tmp_path):
    routes = [{"regexes": [".*"], "extractors": [{"name": "missing"}]}]
    with pytest.raises(ValueError):
        materialize(routes, {}, tmp_path)


def test_materialize_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        materialize([], {"../evil": "x = 1"}, tmp_path)


# ── transform ─────────────────────────────────────────────────────────────────
def test_transform_jsonl_and_array_with_stable_names(tmp_path):
    (tmp_path / "a.jsonl").write_text(
        '{"url": "http://x.com/p1", "title": "One"}\n'
        '{"url": "http://x.com/p2", "title": "Two"}\n'
    )
    (tmp_path / "b.json").write_text('[{"title": "no-url"}]')
    recs = transform.to_records(tmp_path, name_seed="page")
    names = {r["name"] for r in recs}
    assert "http-x.com-p1" in names and "http-x.com-p2" in names
    # the url-less record gets a deterministic hashed name with the seed prefix
    assert any(n.startswith("page-") for n in names)
    assert all(set(r) == {"name", "fields"} for r in recs)


# ── sandbox ───────────────────────────────────────────────────────────────────
def test_sandbox_runs_and_captures_output():
    proc = sandbox.run(["python3", "-c", "print('hi')"], no_network=False, timeout=30)
    assert proc.stdout.strip() == "hi"


def test_sandbox_raises_on_failure():
    with pytest.raises(sandbox.SandboxError):
        sandbox.run(["python3", "-c", "import sys; sys.exit(3)"], no_network=False, timeout=30)


# ── full orchestration (download + extract injected) ─────────────────────────
def test_run_crawl_end_to_end_injected(tmp_path):
    job = CrawlJob.from_fields(_job_fields())

    def fake_download(query, html_dir, *, mode, cmon_bin):
        Path(html_dir).mkdir(parents=True, exist_ok=True)
        f = Path(html_dir) / "0.html"
        f.write_text("<html><body>hi</body></html>")
        return [f]

    # Simulate `cmon extract`: write a JSONL output file into <workdir>/extracted.
    def fake_extract_runner(argv):
        out = tmp_path / "extracted"
        out.mkdir(parents=True, exist_ok=True)
        (out / "0.jsonl").write_text('{"url": "http://example.com/a", "title": "A"}\n')

    records = run_crawl(
        job,
        workdir=tmp_path,
        download_fn=fake_download,
        extract_runner=fake_extract_runner,
    )
    assert len(records) == 1
    assert records[0]["fields"]["title"] == "A"
    assert records[0]["name"] == "http-example.com-a"
