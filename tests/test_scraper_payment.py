"""
Offline tests for the scraper-service payment gate: an x402 (ERC-3009) payment
authorization is embedded in the crawl_job manifest, priced by query.limit, and
verified+settled (locally) before the job runs. No network/chain/cmon needed.
"""

import pytest

pytest.importorskip("eth_account")  # x402 signing/verification needs eth-account

from quickbeam import x402 as x402mod
from quickbeam.crawl.config import CrawlJob
from quickbeam.scraper_service import Service, parse_args

# Deterministic test wallet + operator recipient.
PAYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
PAY_TO = "0x000000000000000000000000000000000000dEaD"


def _service(tmp_path, **extra):
    args = [
        "--crawl-job-schema", "fangorn.crawljob.v1=0xabc",
        "--state-dir", str(tmp_path),
        "--x402-pay-to", PAY_TO,
        "--x402-network", "base-sepolia",
        "--x402-price-base", "0.05",
        "--x402-price-per-unit", "0.001",
    ]
    for k, v in extra.items():
        args += [k, v]
    return Service(parse_args(args))


def _job_fields(limit, payment=None):
    fields = {
        "routes": [{"regexes": [".*"], "extractors": [{"name": "ex1"}]}],
        "extractors": {"ex1": "extractor = 1\n"},
        "query": {"urls": ["example.com"], "matchType": "domain", "limit": limit},
        "outputSchema": "fangorn.webpage.v1",
    }
    if payment is not None:
        fields["paymentReceipt"] = payment
    return fields


def _sign_for(service, job):
    """Sign an x402 authorization for exactly the job's required price."""
    requirements = service.requirements_for(job)
    return x402mod.sign_payment(PAYER_KEY, requirements)


def test_price_scales_with_limit(tmp_path):
    svc = _service(tmp_path)
    # base 0.05 + 0.001 * limit, at 6 decimals
    assert svc.price_atomic_for(0) == 50_000
    assert svc.price_atomic_for(10) == 60_000
    assert svc.price_atomic_for(100) == 150_000


def test_valid_embedded_payment_accepted(tmp_path):
    svc = _service(tmp_path)
    job = CrawlJob.from_fields(_job_fields(10))
    payment = _sign_for(svc, job)
    job_paid = CrawlJob.from_fields(_job_fields(10, payment=payment))
    ok, payer, err = svc.verify_payment(job_paid)
    assert ok and err is None
    assert payer and payer.lower().startswith("0x")


def test_missing_payment_rejected(tmp_path):
    svc = _service(tmp_path)
    job = CrawlJob.from_fields(_job_fields(10))  # no paymentReceipt
    ok, _, err = svc.verify_payment(job)
    assert not ok and "no payment" in err.lower()


def test_underpayment_rejected(tmp_path):
    svc = _service(tmp_path)
    # Sign for a cheap job (limit 1) but submit it as an expensive one (limit 100).
    cheap = CrawlJob.from_fields(_job_fields(1))
    payment = _sign_for(svc, cheap)
    expensive = CrawlJob.from_fields(_job_fields(100, payment=payment))
    ok, _, err = svc.verify_payment(expensive)
    assert not ok and "below the required amount" in err.lower()


def test_wrong_recipient_rejected(tmp_path):
    svc = _service(tmp_path)
    job = CrawlJob.from_fields(_job_fields(10))
    # Sign a valid-looking authorization that pays someone else.
    reqs = svc.requirements_for(job)
    reqs.pay_to = "0x1111111111111111111111111111111111111111"
    payment = x402mod.sign_payment(PAYER_KEY, reqs)
    job_paid = CrawlJob.from_fields(_job_fields(10, payment=payment))
    ok, _, err = svc.verify_payment(job_paid)
    assert not ok and "payto" in err.lower()


def test_no_require_payment_bypasses(tmp_path):
    svc = _service(tmp_path, **{"--x402-network": "base-sepolia"})
    svc.cfg.no_require_payment = True
    job = CrawlJob.from_fields(_job_fields(10))
    ok, _, err = svc.verify_payment(job)
    assert ok and err is None
