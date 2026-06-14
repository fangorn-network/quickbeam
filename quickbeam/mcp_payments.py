"""
mcp_payments.py — Phase 2 x402 gating for MCP tool calls (isolated).

This module exists so the payment story stays *out* of the Phase 1 MCP server.
mcp_server.py calls exactly one function per gated tool — `Gate.charge()` — and
otherwise knows nothing about payments. If payments are disabled the gate is
never constructed and the tools run untouched.

Unlike the HTTP path (quickbeam/x402.py's middleware), MCP has no request
headers, so payment cannot ride on `X-PAYMENT`. Instead each gated tool takes an
optional `payment` argument carrying the same base64-encoded x402 payment
payload. The lifecycle becomes a two-call dance the agent drives:

    1. agent calls tool with no `payment`
         → ChargeResult(ok=False, challenge={payment_required, accepts:[...]})
    2. agent signs the quoted requirement, calls again with `payment=<b64>`
         → ChargeResult(ok=True, settlement={transaction, payer, network})
       and the tool proceeds to do its work.

The verify/settle primitives and price math are reused verbatim from
quickbeam/x402.py — only the transport (tool-arg instead of HTTP header) differs.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from quickbeam.x402 import (
        X402Config, X402Error, X402_VERSION,
        decode_payment_header, price_to_atomic,
    )
except ImportError:  # script-style import from within quickbeam/
    from x402 import (
        X402Config, X402Error, X402_VERSION,
        decode_payment_header, price_to_atomic,
    )


@dataclass
class ChargeResult:
    """Outcome of a payment attempt for one tool call."""
    ok:         bool
    challenge:  dict | None = None   # returned to the agent when payment is needed
    settlement: dict | None = None   # settlement receipt when ok


class Gate:
    """Per-tool x402 charger. Construct once with an X402Config; call charge()
    at the top of each gated tool handler."""

    def __init__(self, config: X402Config):
        self.config   = config
        self.verifier = config.verifier()

    def _challenge(self, requirements, error: str) -> dict:
        return {
            "payment_required": True,
            "x402Version":      X402_VERSION,
            "accepts":          [requirements.to_dict()],
            "error":            error,
        }

    def charge(self, tool_name: str, payment_b64: str | None) -> ChargeResult:
        """Verify + settle a payment for `tool_name`. Returns ok=False with an
        x402 challenge when payment is missing or invalid; ok=True with a
        settlement receipt when the call is paid for."""
        # The resource identifier for an MCP tool is a stable mcp:// URI.
        requirements = self.config.requirements_for(f"mcp://{tool_name}")

        if not payment_b64:
            return ChargeResult(False, challenge=self._challenge(requirements, "payment required"))

        try:
            payment = decode_payment_header(payment_b64)
            result  = self.verifier.verify(payment, requirements)
        except X402Error as exc:
            return ChargeResult(False, challenge=self._challenge(requirements, str(exc)))

        if not result.ok:
            return ChargeResult(False, challenge=self._challenge(
                requirements, result.error or "payment verification failed"))

        try:
            settlement = self.verifier.settle(payment, requirements)
        except Exception as exc:  # noqa: BLE001
            return ChargeResult(False, challenge=self._challenge(
                requirements, f"settlement failed: {exc}"))

        if not settlement.success:
            return ChargeResult(False, challenge=self._challenge(requirements, "settlement failed"))

        return ChargeResult(True, settlement={
            "transaction": settlement.transaction,
            "payer":       settlement.payer,
            "network":     settlement.network,
            "amount":      requirements.max_amount_required,
        })


def build_gate(pay_to: str, price: str, network: str = "base-sepolia",
               asset: str | None = None, decimals: int = 6,
               facilitator_url: str | None = None) -> Gate:
    """Construct a Gate from human-friendly settings (mirrors the serve flags)."""
    config = X402Config(
        pay_to=pay_to,
        price_atomic=price_to_atomic(price, decimals),
        network=network,
        asset=asset or "",
        facilitator_url=facilitator_url,
        description="quickbeam MCP tool call",
    )
    return Gate(config)
