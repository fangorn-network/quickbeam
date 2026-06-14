"""
x402.py — HTTP 402 "Payment Required" support for the quickbeam API.

Implements the x402 v1 protocol (the Coinbase-originated scheme used by
payment-gated HTTP APIs) for the `exact` scheme over an EVM network using
EIP-3009 `transferWithAuthorization` signatures.

This module is intentionally self-contained — it imports nothing from the rest
of quickbeam — so it can be loaded whether server.py is run as a script or
imported as `quickbeam.server`. It is used from three places:

  • server.py  — installs X402Middleware to gate the search / query routes.
  • mcp_server — an agent-side client that signs payments and retries 402s.
  • tests      — simulate an agent paying for a request end to end.

Payment lifecycle
-----------------
  1. Client requests a gated route with no `X-PAYMENT` header.
  2. Server replies 402 with a JSON body: {x402Version, accepts:[reqs], error}.
  3. Client picks a requirement, signs an EIP-712 TransferWithAuthorization,
     base64-encodes the payment object into the `X-PAYMENT` header, and retries.
  4. Server verifies the signature (locally, or via a facilitator), optionally
     settles on-chain, then serves the response with an `X-PAYMENT-RESPONSE`
     header describing settlement.

Verification is pluggable (see Verifier). The default LocalVerifier recovers
the EIP-712 signer and checks the authorization without broadcasting — enough
for testnets, demos, and the test-suite. Point `--x402-facilitator` at a real
facilitator to verify + settle on-chain.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

X402_VERSION = 1

# ---------------------------------------------------------------------------
# NETWORKS — chainId + a sensible default USDC contract per network.
# ---------------------------------------------------------------------------
NETWORKS: dict[str, dict] = {
    "base-sepolia": {
        "chain_id": 84532,
        "usdc":     "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    },
    "base": {
        "chain_id": 8453,
        "usdc":     "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
    "avalanche-fuji": {
        "chain_id": 43113,
        "usdc":     "0x5425890298aed601595a70AB815c96711a31Bc65",
    },
}


class X402Error(Exception):
    """Raised when a payment is malformed or fails verification."""


# ---------------------------------------------------------------------------
# DATA SHAPES
# ---------------------------------------------------------------------------
@dataclass
class PaymentRequirements:
    """One entry in the 402 `accepts` array."""
    scheme:            str
    network:           str
    max_amount_required: str          # atomic units, as a string
    pay_to:            str
    asset:             str
    resource:          str
    description:       str  = ""
    mime_type:         str  = "application/json"
    max_timeout_seconds: int = 60
    extra:             dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "scheme":            self.scheme,
            "network":           self.network,
            "maxAmountRequired": self.max_amount_required,
            "resource":          self.resource,
            "description":       self.description,
            "mimeType":          self.mime_type,
            "payTo":             self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "asset":             self.asset,
            "extra":             self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaymentRequirements":
        return cls(
            scheme=d["scheme"],
            network=d["network"],
            max_amount_required=str(d["maxAmountRequired"]),
            pay_to=d["payTo"],
            asset=d["asset"],
            resource=d.get("resource", ""),
            description=d.get("description", ""),
            mime_type=d.get("mimeType", "application/json"),
            max_timeout_seconds=int(d.get("maxTimeoutSeconds", 60)),
            extra=d.get("extra", {}) or {},
        )


@dataclass
class VerifyResult:
    ok:    bool
    payer: str | None = None
    error: str | None = None


@dataclass
class SettleResult:
    success:     bool
    transaction: str
    network:     str
    payer:       str | None = None

    def to_header(self) -> str:
        return _b64encode_json({
            "success":     self.success,
            "transaction": self.transaction,
            "network":     self.network,
            "payer":       self.payer,
        })


# ---------------------------------------------------------------------------
# HEADER ENCODING
# ---------------------------------------------------------------------------
def _b64encode_json(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def _b64decode_json(s: str) -> dict:
    return json.loads(base64.b64decode(s).decode())


def decode_payment_header(header: str) -> dict:
    """Decode an `X-PAYMENT` header into the payment object."""
    try:
        payment = _b64decode_json(header)
    except Exception as exc:  # noqa: BLE001
        raise X402Error(f"X-PAYMENT header is not valid base64 JSON: {exc}") from exc
    if payment.get("x402Version") != X402_VERSION:
        raise X402Error(f"unsupported x402Version: {payment.get('x402Version')!r}")
    if "payload" not in payment or "authorization" not in payment.get("payload", {}):
        raise X402Error("payment payload missing 'authorization'")
    return payment


# ---------------------------------------------------------------------------
# EIP-712 (transferWithAuthorization) — shared by signer and verifier.
# ---------------------------------------------------------------------------
def _typed_data(requirements: PaymentRequirements, authorization: dict) -> dict:
    net = NETWORKS.get(requirements.network)
    if not net:
        raise X402Error(f"unknown network: {requirements.network!r}")
    extra = requirements.extra or {}
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name",              "type": "string"},
                {"name": "version",           "type": "string"},
                {"name": "chainId",           "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from",        "type": "address"},
                {"name": "to",          "type": "address"},
                {"name": "value",       "type": "uint256"},
                {"name": "validAfter",  "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce",       "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name":              extra.get("name", "USD Coin"),
            "version":           extra.get("version", "2"),
            "chainId":           net["chain_id"],
            "verifyingContract": requirements.asset,
        },
        "message": {
            "from":        authorization["from"],
            "to":          authorization["to"],
            "value":       int(authorization["value"]),
            "validAfter":  int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce":       authorization["nonce"],
        },
    }


# ---------------------------------------------------------------------------
# AGENT SIDE — build + sign a payment for a chosen requirement.
# ---------------------------------------------------------------------------
def sign_payment(private_key: str, requirements: PaymentRequirements,
                 valid_for_seconds: int | None = None) -> dict:
    """
    Sign an EIP-3009 transferWithAuthorization authorizing payment of exactly
    `maxAmountRequired` to `payTo`. Returns the payment object ready to be put
    into the `X-PAYMENT` header (see encode_payment_header).

    Requires eth-account. Raises X402Error if it is not installed.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except ImportError as exc:  # noqa: BLE001
        raise X402Error(
            "eth-account is required to sign x402 payments. Install with: pip install eth-account"
        ) from exc

    acct = Account.from_key(private_key)
    now  = int(time.time())
    valid_for = valid_for_seconds or requirements.max_timeout_seconds
    authorization = {
        "from":        acct.address,
        "to":          requirements.pay_to,
        "value":       str(requirements.max_amount_required),
        "validAfter":  str(now - 60),
        "validBefore": str(now + valid_for),
        # 32-byte random nonce — uniqueness prevents authorization replay.
        "nonce":       "0x" + secrets.token_hex(32),
    }

    typed = _typed_data(requirements, authorization)
    signable = encode_typed_data(full_message=typed)
    signed   = acct.sign_message(signable)

    return {
        "x402Version": X402_VERSION,
        "scheme":      requirements.scheme,
        "network":     requirements.network,
        "payload": {
            "signature":     signed.signature.hex()
                             if isinstance(signed.signature, (bytes, bytearray))
                             else signed.signature,
            "authorization": authorization,
        },
    }


def encode_payment_header(payment: dict) -> str:
    """Base64-encode a payment object for the `X-PAYMENT` header."""
    return _b64encode_json(payment)


# ---------------------------------------------------------------------------
# SERVER SIDE — verifiers.
# ---------------------------------------------------------------------------
class Verifier:
    """Strategy interface: verify a decoded payment + settle it."""

    def verify(self, payment: dict, requirements: PaymentRequirements) -> VerifyResult:  # noqa: D401
        raise NotImplementedError

    def settle(self, payment: dict, requirements: PaymentRequirements) -> SettleResult:
        raise NotImplementedError


class LocalVerifier(Verifier):
    """
    Verify the EIP-712 signature locally and check the authorization terms,
    without contacting a chain or facilitator. Settlement is recorded but not
    broadcast — suitable for testnets, demos, and tests. Swap in
    FacilitatorVerifier for real on-chain settlement.
    """

    def verify(self, payment: dict, requirements: PaymentRequirements) -> VerifyResult:
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
        except ImportError as exc:  # noqa: BLE001
            raise X402Error("eth-account is required for local verification") from exc

        if payment.get("scheme") != requirements.scheme:
            return VerifyResult(False, error=f"scheme mismatch: {payment.get('scheme')!r}")
        if payment.get("network") != requirements.network:
            return VerifyResult(False, error=f"network mismatch: {payment.get('network')!r}")

        payload = payment["payload"]
        auth    = payload["authorization"]
        sig     = payload["signature"]

        # Terms: paying the right recipient, at least the required amount, in window.
        if auth.get("to", "").lower() != requirements.pay_to.lower():
            return VerifyResult(False, error="authorization 'to' does not match payTo")
        try:
            if int(auth["value"]) < int(requirements.max_amount_required):
                return VerifyResult(False, error="authorized value is below the required amount")
        except (KeyError, ValueError):
            return VerifyResult(False, error="authorization 'value' is invalid")

        now = int(time.time())
        if int(auth.get("validAfter", 0)) > now:
            return VerifyResult(False, error="authorization not yet valid")
        if int(auth.get("validBefore", 0)) < now:
            return VerifyResult(False, error="authorization expired")

        # Recover the signer and confirm it matches the declared payer.
        try:
            typed     = _typed_data(requirements, auth)
            signable  = encode_typed_data(full_message=typed)
            recovered = Account.recover_message(signable, signature=sig)
        except Exception as exc:  # noqa: BLE001
            return VerifyResult(False, error=f"signature recovery failed: {exc}")

        if recovered.lower() != auth.get("from", "").lower():
            return VerifyResult(False, error="signature does not match authorization 'from'")

        return VerifyResult(True, payer=recovered)

    def settle(self, payment: dict, requirements: PaymentRequirements) -> SettleResult:
        auth = payment["payload"]["authorization"]
        # No broadcast in local mode — synthesise a settlement receipt keyed off
        # the (unique) authorization nonce so it is stable and traceable.
        return SettleResult(
            success=True,
            transaction="local:" + auth.get("nonce", "0x0"),
            network=requirements.network,
            payer=auth.get("from"),
        )


class FacilitatorVerifier(Verifier):
    """Delegate verify + settle to an x402 facilitator over HTTP."""

    def __init__(self, facilitator_url: str):
        self.url = facilitator_url.rstrip("/")

    def _post(self, path: str, payment: dict, requirements: PaymentRequirements) -> dict:
        import requests  # local import — only needed in facilitator mode
        body = {
            "x402Version":         X402_VERSION,
            "paymentPayload":      payment,
            "paymentRequirements": requirements.to_dict(),
        }
        resp = requests.post(f"{self.url}{path}", json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def verify(self, payment: dict, requirements: PaymentRequirements) -> VerifyResult:
        data = self._post("/verify", payment, requirements)
        return VerifyResult(
            ok=bool(data.get("isValid")),
            payer=data.get("payer"),
            error=data.get("invalidReason"),
        )

    def settle(self, payment: dict, requirements: PaymentRequirements) -> SettleResult:
        data = self._post("/settle", payment, requirements)
        return SettleResult(
            success=bool(data.get("success")),
            transaction=data.get("transaction", ""),
            network=data.get("network", requirements.network),
            payer=data.get("payer"),
        )


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
@dataclass
class X402Config:
    """Resolved payment-gating configuration for the server."""
    pay_to:      str
    price_atomic: int                       # price per gated request, atomic units
    network:     str  = "base-sepolia"
    asset:       str  = ""                  # token contract; defaults to network USDC
    scheme:      str  = "exact"
    description: str  = "quickbeam search query"
    token_name:  str  = "USD Coin"
    token_version: str = "2"
    facilitator_url: str | None = None
    # Exact paths (method-agnostic) that require payment.
    gated_paths: set[str] = field(default_factory=lambda: {
        "/search", "/search/vector", "/search/text",
    })

    def __post_init__(self):
        net = NETWORKS.get(self.network)
        if not net:
            raise ValueError(f"unknown x402 network {self.network!r}; known: {list(NETWORKS)}")
        if not self.asset:
            self.asset = net["usdc"]

    def verifier(self) -> Verifier:
        return FacilitatorVerifier(self.facilitator_url) if self.facilitator_url else LocalVerifier()

    def requirements_for(self, resource_url: str) -> PaymentRequirements:
        return PaymentRequirements(
            scheme=self.scheme,
            network=self.network,
            max_amount_required=str(self.price_atomic),
            pay_to=self.pay_to,
            asset=self.asset,
            resource=resource_url,
            description=self.description,
            extra={"name": self.token_name, "version": self.token_version},
        )

    def is_gated(self, path: str) -> bool:
        return path in self.gated_paths


# ---------------------------------------------------------------------------
# MIDDLEWARE
# ---------------------------------------------------------------------------
def build_middleware(config: X402Config):
    """
    Return a Starlette BaseHTTPMiddleware subclass instance-factory bound to
    `config`. Use as: app.add_middleware(X402Middleware, config=config).
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    verifier = config.verifier()

    class X402Middleware(BaseHTTPMiddleware):
        def __init__(self, app, config: X402Config = config):
            super().__init__(app)
            self.config   = config
            self.verifier = verifier

        def _402(self, requirements: PaymentRequirements, error: str) -> JSONResponse:
            return JSONResponse(
                status_code=402,
                content={
                    "x402Version": X402_VERSION,
                    "accepts":     [requirements.to_dict()],
                    "error":       error,
                },
            )

        async def dispatch(self, request, call_next):
            if not self.config.is_gated(request.url.path):
                return await call_next(request)

            requirements = self.config.requirements_for(str(request.url))
            header = request.headers.get("X-PAYMENT")
            if not header:
                return self._402(requirements, "X-PAYMENT header is required")

            try:
                payment = decode_payment_header(header)
                result  = self.verifier.verify(payment, requirements)
            except X402Error as exc:
                return self._402(requirements, str(exc))

            if not result.ok:
                return self._402(requirements, result.error or "payment verification failed")

            try:
                settlement = self.verifier.settle(payment, requirements)
            except Exception as exc:  # noqa: BLE001
                return self._402(requirements, f"settlement failed: {exc}")

            if not settlement.success:
                return self._402(requirements, "settlement failed")

            response = await call_next(request)
            response.headers["X-PAYMENT-RESPONSE"] = settlement.to_header()
            return response

    return X402Middleware


# ---------------------------------------------------------------------------
# AGENT HELPER — pay-and-retry over an httpx client.
# ---------------------------------------------------------------------------
def decode_settlement_header(header: str) -> dict:
    return _b64decode_json(header)


def select_requirement(body_402: dict, network: str | None = None,
                       scheme: str = "exact") -> PaymentRequirements:
    """Pick a payment requirement from a 402 body, preferring a matching network."""
    accepts = body_402.get("accepts", [])
    if not accepts:
        raise X402Error("402 response has no 'accepts' requirements")
    chosen = None
    for a in accepts:
        if a.get("scheme") == scheme and (network is None or a.get("network") == network):
            chosen = a
            break
    chosen = chosen or accepts[0]
    return PaymentRequirements.from_dict(chosen)


def price_to_atomic(price: str | float, decimals: int = 6) -> int:
    """Convert a human price (e.g. '0.001' USDC) to atomic units."""
    from decimal import Decimal
    return int(Decimal(str(price)) * (10 ** decimals))


# ---------------------------------------------------------------------------
# PAYING CLIENT — wraps an httpx.AsyncClient with automatic 402 pay-and-retry.
# This is the agent side: on a 402, it reads the requirements, signs a payment
# with the configured wallet, and replays the request with the X-PAYMENT header.
# Used by the MCP server and the test-suite.
# ---------------------------------------------------------------------------
@dataclass
class PaymentRecord:
    resource:    str
    amount:      str
    network:     str
    transaction: str | None = None
    payer:       str | None = None


class PaymentRequired(X402Error):
    """Raised when a 402 cannot be satisfied (no wallet, or retry still 402)."""


class PayingClient:
    """
    Thin wrapper over an httpx.AsyncClient that transparently pays x402-gated
    endpoints. Construct with a wallet private key; every request that comes
    back 402 is paid for and retried exactly once.

    Tracks each settled payment in `.payments` so callers (and tests) can assert
    on what was spent.
    """

    def __init__(self, base_url: str, private_key: str | None,
                 network: str | None = None, timeout: float = 60.0,
                 max_price_atomic: int | None = None, transport=None):
        import httpx
        self.base_url = base_url.rstrip("/")
        self.private_key = private_key
        self.network = network
        self.max_price_atomic = max_price_atomic
        self.payments: list[PaymentRecord] = []
        # `transport` lets tests inject an ASGITransport pointed at the app.
        kwargs = {"base_url": self.base_url, "timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs):
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code != 402:
            return resp

        if not self.private_key:
            raise PaymentRequired(
                f"{method} {path} requires payment but no wallet is configured"
            )

        body = resp.json()
        requirements = select_requirement(body, network=self.network)

        if self.max_price_atomic is not None and \
           int(requirements.max_amount_required) > self.max_price_atomic:
            raise PaymentRequired(
                f"price {requirements.max_amount_required} exceeds max "
                f"{self.max_price_atomic} for {path}"
            )

        payment = sign_payment(self.private_key, requirements)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-PAYMENT"] = encode_payment_header(payment)

        paid = await self._client.request(method, path, headers=headers, **kwargs)

        record = PaymentRecord(
            resource=requirements.resource,
            amount=requirements.max_amount_required,
            network=requirements.network,
        )
        settle_hdr = paid.headers.get("X-PAYMENT-RESPONSE")
        if settle_hdr:
            settlement = decode_settlement_header(settle_hdr)
            record.transaction = settlement.get("transaction")
            record.payer       = settlement.get("payer")
        self.payments.append(record)
        return paid

    async def get(self, path: str, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs):
        return await self.request("POST", path, **kwargs)
