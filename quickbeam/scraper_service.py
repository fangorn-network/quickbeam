"""
quickbeam scraper service — on-demand Common Crawl scraping driven by Fangorn.

Pipeline:
  1. A `crawl_job` manifest is published on Fangorn (by the agentic UI / a user):
     it carries CmonCrawl routes + extractor sources + a crawl query + the target
     output schema (see quickbeam/crawl/config.py).
  2. This service polls the subgraph for `ManifestPublished` events for the
     crawl_job schema (block_gt cursor + checkpoint, like `quickbeam watch`).
  3. For each new job: verify the embedded payment authorization, resolve the
     job + extractor sources from IPFS, run the crawl in a sandbox
     (quickbeam.crawl.run_crawl), and publish the resulting records back to
     Fangorn under the output schema (quickbeam/fangorn_publish.py).
  4. The existing `quickbeam watch --bundle <outputSchema>=0x…` + `serve`/`mcp`
     stack then embeds and serves the new dataset. No new code there.

Payment (pay for compute): the trigger is on-chain, so payment travels inside the
manifest. Before publishing, the client signs an x402 ERC-3009
transferWithAuthorization for the exact price and embeds it in the crawl_job field
`paymentReceipt`. The listener recomputes the price from the job's own query.limit
(price = base + per_unit*limit), then verifies + settles that authorization
(quickbeam/x402.py) before running — local verify on testnet, or via
--x402-facilitator for on-chain settlement. Override with --no-require-payment for
dev. Selling access to the *produced* dataset is a separate concern (Fangorn's
SettlementRegistry / x402 on serve+mcp), not this gate. Operators advertise price
params at GET /pricing.

CmonCrawl must be installed in its own venv (it pins an old pydantic); point the
service at that binary with --cmon-bin / $CMON_BIN.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import aiohttp
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from quickbeam import x402 as x402mod
from quickbeam.crawl import run_crawl
from quickbeam.crawl.config import CrawlJob
from quickbeam.crawl.pipeline import SandboxLimits
from quickbeam.fangorn_publish import publish_records, PublishError

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

FALLBACK_GATEWAYS = [
    "https://gateway.pinata.cloud/ipfs",
    "https://ipfs.io/ipfs",
    "https://dweb.link/ipfs",
    "https://w3s.link/ipfs",
]

PUBLISHES_QUERY = """
query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!, $blockGt: BigInt!) {
  manifestPublisheds(
    where: { schemaId: $schemaId, blockNumber_gt: $blockGt }
    first: $first skip: $skip
    orderBy: blockNumber orderDirection: asc
  ) { id owner schemaId name manifestCid blockNumber blockTimestamp }
}
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Quickbeam scraper service — Common Crawl scraping via Fangorn-registered jobs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--crawl-job-schema", required=True, metavar="NAME=0x...",
                   help="The crawl_job schema to watch, as name=schemaId.")
    p.add_argument("--subgraph-url",
                   default="https://gateway.thegraph.com/api/subgraphs/id/8SgbhtiitpAhEfyTgeAHxHH5DQ2gTygUuXgc3b7MCFyc")
    p.add_argument("--graph-api-key", default=os.environ.get("GRAPH_API_KEY", ""))
    p.add_argument("--ipfs-gateway", default="https://gateway.pinata.cloud/ipfs")
    p.add_argument("--poll-interval", type=int, default=60)
    p.add_argument("--ipfs-timeout", type=int, default=30)
    p.add_argument("--state-dir", default="./db/scraper",
                   help="Directory for checkpoint, paid-ledger, and job-status files.")

    # crawl execution
    p.add_argument("--cmon-bin", default=os.environ.get("CMON_BIN", "cmon"))
    p.add_argument("--workdir", default=None, help="Scratch dir for crawl jobs (default: per-job tmp).")
    p.add_argument("--no-sandbox-net-isolation", action="store_true",
                   help="Disable network-namespace isolation for the extract step.")
    p.add_argument("--job-timeout", type=int, default=1200)
    p.add_argument("--job-mem-mb", type=int, default=2048)

    # publishing
    p.add_argument("--node-bin", default="node")
    p.add_argument("--publish-script", default=None, help="Path to src/publish.mjs (default: repo copy).")
    p.add_argument("--chunk-size", type=int, default=1000)

    # payment — an x402 ERC-3009 authorization embedded in the crawl_job manifest
    # (field `paymentReceipt`). The publisher signs it for exactly the price this
    # operator advertises (GET /pricing) before publishing; the listener verifies
    # + settles it before running. Price scales with the requested query.limit:
    #     price = base + per_unit * limit   (whole token units → atomic at settle)
    p.add_argument("--no-require-payment", action="store_true",
                   help="Run jobs without a payment authorization (dev only).")
    x = p.add_argument_group("x402 payment (embedded in the crawl_job manifest)")
    x.add_argument("--x402-pay-to", default=None, metavar="0x...",
                   help="Operator recipient address. Enables payment when set.")
    x.add_argument("--x402-price-base", default="0.05", metavar="USDC",
                   help="Flat base price per job.")
    x.add_argument("--x402-price-per-unit", default="0.001", metavar="USDC",
                   help="Added per unit of query.limit.")
    x.add_argument("--x402-network", default="base-sepolia")
    x.add_argument("--x402-asset", default=None, metavar="0x...")
    x.add_argument("--x402-decimals", type=int, default=6)
    x.add_argument("--x402-facilitator", default=None, metavar="URL",
                   help="Facilitator for on-chain verify+settle. Omit for local (testnet) verify.")

    # server
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# STATE (checkpoint, paid-ledger, job status) — small JSON files under state-dir.
# A lock guards them since the FastAPI handlers and the poller task both write.
# ---------------------------------------------------------------------------

class State:
    def __init__(self, state_dir: str):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._checkpoint = self._read("checkpoint.json", {"last_block": 0, "done_cids": []})
        self._jobs = self._read("jobs.json", {})           # job_id -> status record

    def _path(self, name: str) -> Path:
        return self.dir / name

    def _read(self, name: str, default):
        try:
            return json.loads(self._path(name).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _write(self, name: str, obj) -> None:
        tmp = self._path(name + ".tmp")
        tmp.write_text(json.dumps(obj))
        tmp.replace(self._path(name))

    # checkpoint ----------------------------------------------------------------
    @property
    def last_block(self) -> int:
        return int(self._checkpoint.get("last_block", 0))

    def is_done(self, manifest_cid: str) -> bool:
        return manifest_cid in self._checkpoint.get("done_cids", [])

    def mark_done(self, manifest_cid: str, block: int) -> None:
        with self._lock:
            cids = self._checkpoint.setdefault("done_cids", [])
            if manifest_cid not in cids:
                cids.append(manifest_cid)
            self._checkpoint["last_block"] = max(self.last_block, int(block))
            self._write("checkpoint.json", self._checkpoint)

    # job status ----------------------------------------------------------------
    def set_job(self, job_id: str, **fields) -> dict:
        with self._lock:
            rec = self._jobs.get(job_id, {"id": job_id})
            rec.update(fields)
            rec["updated"] = int(time.time())
            self._jobs[job_id] = rec
            self._write("jobs.json", self._jobs)
            return rec

    def get_job(self, job_id: str) -> dict | None:
        return self._jobs.get(job_id)


# ---------------------------------------------------------------------------
# IPFS — fetch manifest + chunks, resolve to the job's `fields` dicts.
# Mirrors the manifest shape quickbeam/server.py already consumes:
# manifest.entries[].fields.dataCid -> chunk (list of records | record).
# ---------------------------------------------------------------------------

async def _fetch(session: aiohttp.ClientSession, gateways: list[str], cid: str, timeout: int):
    for i, base in enumerate(gateways):
        try:
            async with session.get(f"{base.rstrip('/')}/{cid}",
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 429:
                    continue
                resp.raise_for_status()
                return json.loads(await resp.text())
        except Exception:
            if i == len(gateways) - 1:
                return None
    return None


def _chunk_records(payload) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("fields"), dict):
            return [payload]
        return [{"fields": payload}]
    return []


async def resolve_job_fields(session, gateways, manifest_cid: str, timeout: int) -> list[dict]:
    """Resolve a crawl_job manifest CID to the list of job `fields` dicts."""
    manifest = await _fetch(session, gateways, manifest_cid, timeout)
    if not isinstance(manifest, dict):
        return []
    out: list[dict] = []
    entries = manifest.get("entries", [])
    for entry in entries:
        fields = entry.get("fields", {}) if isinstance(entry, dict) else {}
        dcid = fields.get("dataCid") if isinstance(fields, dict) else None
        if dcid:
            chunk = await _fetch(session, gateways, dcid, timeout)
            for rec in _chunk_records(chunk):
                f = rec.get("fields", rec)
                if isinstance(f, dict):
                    out.append(f)
        elif isinstance(fields, dict) and fields:
            out.append(fields)
    return out


async def resolve_extractor_source(session, gateways, cid: str, timeout: int) -> str | None:
    """Fetch an extractor's Python source by CID (raw text, or JSON {source})."""
    for i, base in enumerate(gateways):
        try:
            async with session.get(f"{base.rstrip('/')}/{cid}",
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 429:
                    continue
                resp.raise_for_status()
                text = await resp.text()
        except Exception:
            if i == len(gateways) - 1:
                return None
            continue
        stripped = text.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    src = obj.get("source") or (obj.get("fields", {}) or {}).get("source")
                    if isinstance(src, str):
                        return src
            except json.JSONDecodeError:
                pass
        return text
    return None


# ---------------------------------------------------------------------------
# JOB EXECUTION
# ---------------------------------------------------------------------------

class Service:
    def __init__(self, cfg: argparse.Namespace):
        self.cfg = cfg
        name, _, schema_id = cfg.crawl_job_schema.partition("=")
        if not schema_id:
            raise ValueError("--crawl-job-schema must be NAME=0xSCHEMAID")
        self.schema_name = name.strip()
        self.schema_id = schema_id.strip().lower()
        self.gateways = [cfg.ipfs_gateway] + [g for g in FALLBACK_GATEWAYS if g != cfg.ipfs_gateway]
        self.state = State(cfg.state_dir)
        self.payment_on = bool(cfg.x402_pay_to)
        self.asset = cfg.x402_asset or x402mod.NETWORKS.get(cfg.x402_network, {}).get("usdc", "")
        self.verifier = (
            x402mod.FacilitatorVerifier(cfg.x402_facilitator)
            if cfg.x402_facilitator else x402mod.LocalVerifier()
        ) if self.payment_on else None

    # ── pricing ───────────────────────────────────────────────────────────────
    def price_atomic_for(self, limit: int) -> int:
        """price = base + per_unit * limit, in atomic units (deterministic, known
        to the client before they sign — limit caps the crawl, so the max cost is
        bounded up front)."""
        base = x402mod.price_to_atomic(self.cfg.x402_price_base, self.cfg.x402_decimals)
        per = x402mod.price_to_atomic(self.cfg.x402_price_per_unit, self.cfg.x402_decimals)
        return base + per * max(0, int(limit))

    def requirements_for(self, job: CrawlJob, resource: str = "crawl_job") -> x402mod.PaymentRequirements:
        return x402mod.PaymentRequirements(
            scheme="exact",
            network=self.cfg.x402_network,
            max_amount_required=str(self.price_atomic_for(job.query.limit)),
            pay_to=self.cfg.x402_pay_to,
            asset=self.asset,
            resource=resource,
            description="quickbeam crawl job",
            extra={"name": "USD Coin", "version": "2"},
        )

    def pricing_info(self) -> dict:
        return {
            "enabled": self.payment_on,
            "payTo": self.cfg.x402_pay_to,
            "network": self.cfg.x402_network,
            "asset": self.asset,
            "decimals": self.cfg.x402_decimals,
            "base": self.cfg.x402_price_base,
            "perUnit": self.cfg.x402_price_per_unit,
            "formula": "price = base + perUnit * query.limit",
            "scheme": "exact",
            "note": "Sign an x402 (ERC-3009 transferWithAuthorization) for the exact "
                    "amount to payTo and embed it in the crawl_job manifest field "
                    "'paymentReceipt' before publishing.",
        }

    def verify_payment(self, job: CrawlJob) -> tuple[bool, str | None, str | None]:
        """Verify (and settle) the payment authorization embedded in the manifest.
        Returns (ok, payer, error)."""
        if not self.payment_on or self.cfg.no_require_payment:
            return True, None, None
        payment = job.payment_object()
        if not payment:
            return False, None, "no payment authorization in manifest 'paymentReceipt'"
        requirements = self.requirements_for(job)
        try:
            result = self.verifier.verify(payment, requirements)
            if not result.ok:
                return False, None, result.error or "payment verification failed"
            settlement = self.verifier.settle(payment, requirements)
            if not settlement.success:
                return False, result.payer, "settlement failed"
            return True, result.payer, None
        except x402mod.X402Error as exc:
            return False, None, str(exc)

    # ── subgraph ────────────────────────────────────────────────────────────
    async def _query_subgraph(self, session, variables: dict) -> dict:
        headers = {}
        if self.cfg.graph_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.graph_api_key}"
        async with session.post(self.cfg.subgraph_url,
                                json={"query": PUBLISHES_QUERY, "variables": variables},
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if "errors" in data:
                raise RuntimeError(f"subgraph error: {data['errors']}")
            return data["data"]

    async def _fetch_new_events(self, session) -> list[dict]:
        events, skip = [], 0
        block_gt = self.state.last_block
        while True:
            data = await self._query_subgraph(session, {
                "schemaId": self.schema_id, "first": 100, "skip": skip, "blockGt": str(block_gt),
            })
            batch = data.get("manifestPublisheds", [])
            events.extend(batch)
            if len(batch) < 100:
                break
            skip += 100
        return events

    # ── one poll cycle ──────────────────────────────────────────────────────
    async def poll_once(self, session) -> int:
        events = await self._fetch_new_events(session)
        ran = 0
        for ev in events:
            mcid = ev.get("manifestCid")
            block = int(ev.get("blockNumber", 0))
            if not mcid or self.state.is_done(mcid):
                continue
            try:
                job_fields = await resolve_job_fields(session, self.gateways, mcid, self.cfg.ipfs_timeout)
                for fields in job_fields:
                    ran += await self._maybe_run_job(session, ev, fields)
            except Exception as exc:  # noqa: BLE001 — one bad job mustn't stop the loop
                print(f"[scraper] error processing manifest {mcid}: {exc}", file=sys.stderr)
            finally:
                self.state.mark_done(mcid, block)
        return ran

    async def _maybe_run_job(self, session, ev: dict, fields: dict) -> int:
        try:
            job = CrawlJob.from_fields(fields)
        except ValueError as exc:
            print(f"[scraper] skipping non-job entry: {exc}")
            return 0

        # Verify (and settle) the payment authorization embedded in the manifest.
        # Price is recomputed from the job's own query.limit, so a client who
        # signed for less than base + per_unit*limit (or to the wrong recipient)
        # fails here. Manifest-level dedupe (done_cids) prevents double-settle.
        ok, payer, err = self.verify_payment(job)
        if not ok:
            print(f"[scraper] job rejected — payment invalid: {err} "
                  f"(price = {self.price_atomic_for(job.query.limit)} atomic units; "
                  f"see GET /pricing)")
            return 0

        job_id = uuid.uuid4().hex[:16]
        self.state.set_job(job_id, status="running", schema=job.output_schema,
                           owner=ev.get("owner"), manifestCid=ev.get("manifestCid"),
                           payer=payer, paymentKey=job.payment_key())
        print(f"[scraper] running job {job_id} → output schema {job.output_schema}")
        try:
            # Resolve any extractor sources referenced only by CID.
            await self._hydrate_extractors(session, job)
            records = await asyncio.get_event_loop().run_in_executor(None, self._run_and_publish, job)
            self.state.set_job(job_id, status="published", records=len(records["records"]),
                               manifestUri=records["manifestUri"], dataset=records.get("dataset"))
            print(f"[scraper] job {job_id} published {records['manifestUri']}")
            return 1
        except Exception as exc:  # noqa: BLE001
            self.state.set_job(job_id, status="failed", error=str(exc)[:500])
            print(f"[scraper] job {job_id} failed: {exc}", file=sys.stderr)
            return 0

    async def _hydrate_extractors(self, session, job: CrawlJob) -> None:
        for name in job.extractor_names():
            if name in job.extractor_sources:
                continue
            cid = job.extractor_refs.get(name)
            if not cid:
                raise ValueError(f"extractor {name!r} has neither inline source nor a CID ref")
            src = await resolve_extractor_source(session, self.gateways, cid, self.cfg.ipfs_timeout)
            if not src:
                raise ValueError(f"could not resolve extractor {name!r} source from CID {cid}")
            job.extractor_sources[name] = src

    def _run_and_publish(self, job: CrawlJob) -> dict:
        """Blocking: crawl (sandboxed) then publish. Runs in an executor thread."""
        limits = SandboxLimits(
            timeout=self.cfg.job_timeout,
            cpu_s=max(60, self.cfg.job_timeout - 30),
            mem_mb=self.cfg.job_mem_mb,
            no_network=not self.cfg.no_sandbox_net_isolation,
        )
        records = run_crawl(job, workdir=self.cfg.workdir, cmon_bin=self.cfg.cmon_bin, limits=limits)
        if not records:
            raise RuntimeError("crawl produced no records")
        result = publish_records(
            records,
            schema_name=job.output_schema,
            schema_def=job.output_schema_def,
            chunk_size=self.cfg.chunk_size,
            node_bin=self.cfg.node_bin,
            publish_script=self.cfg.publish_script,
        )
        return {"records": records, "manifestUri": result.get("manifestUri"), "dataset": result.get("dataset")}

    # ── background poller ─────────────────────────────────────────────────────
    async def run_poller(self):
        print(f"[scraper] watching crawl_job schema {self.schema_name}={self.schema_id}")
        print(f"[scraper] poll interval {self.cfg.poll_interval}s; payment "
              f"{'OPTIONAL (dev)' if self.cfg.no_require_payment else 'REQUIRED via x402'}")
        async with aiohttp.ClientSession() as session:
            cycle = 0
            while True:
                cycle += 1
                try:
                    ran = await self.poll_once(session)
                    print(f"[scraper] cycle {cycle}: {ran} job(s) executed (last block {self.state.last_block})")
                except Exception as exc:  # noqa: BLE001
                    print(f"[scraper] cycle {cycle} error: {exc}", file=sys.stderr)
                await asyncio.sleep(self.cfg.poll_interval)


# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------

class PriceRequest(BaseModel):
    # The intended job fields (or at least {"query": {"limit": N}}) so the caller
    # can read back the exact price to sign for before publishing.
    job: dict


def build_app(service: Service) -> FastAPI:
    app = FastAPI(title="quickbeam scraper service")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    if service.payment_on:
        print(f"[scraper] payment ON: base {service.cfg.x402_price_base} + "
              f"{service.cfg.x402_price_per_unit}/limit USDC to {service.cfg.x402_pay_to} "
              f"on {service.cfg.x402_network} "
              f"({'facilitator' if service.cfg.x402_facilitator else 'local verify'})")

    @app.on_event("startup")
    async def _start():
        asyncio.create_task(service.run_poller())

    @app.get("/health")
    async def health():
        return {"ok": True, "schema": service.schema_name, "lastBlock": service.state.last_block}

    @app.get("/pricing")
    async def pricing():
        """Free: how to pay. The client signs an x402 authorization for the exact
        price and embeds it in the crawl_job manifest's `paymentReceipt`."""
        return service.pricing_info()

    @app.post("/pricing/quote")
    async def pricing_quote(body: PriceRequest):
        """Compute the exact atomic price for a specific job (by its query.limit)."""
        try:
            job = CrawlJob.from_fields(body.job)
        except ValueError as exc:
            return {"error": f"invalid job: {exc}"}
        return {
            "priceAtomic": str(service.price_atomic_for(job.query.limit)),
            "limit": job.query.limit,
            "requirements": service.requirements_for(job).to_dict(),
        }

    @app.get("/jobs/{job_id}")
    async def job_status(job_id: str):
        rec = service.state.get_job(job_id)
        return rec or {"error": "unknown job id", "id": job_id}

    return app


def main():
    cfg = parse_args()
    service = Service(cfg)
    app = build_app(service)
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
