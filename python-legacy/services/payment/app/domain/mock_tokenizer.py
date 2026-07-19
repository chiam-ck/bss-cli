# ============================================================================
#  SANDBOX ONLY — NEVER STORE PAN OR CVV IN A REAL SYSTEM
#
#  In production, card numbers go directly from the client (mobile app, web
#  portal) to a PCI-compliant tokenization service (Stripe, Adyen, Checkout.com,
#  etc). The backend ONLY ever sees the resulting token.
#
#  This mock tokenizes server-side PURELY for demo simplicity and MUST NOT
#  be used as a production pattern. The public POST /paymentMethod endpoint
#  takes a pre-tokenized request — this module is only invoked from tests
#  and from `bss payment add-card` in dev mode (gated behind
#  BSS_ENABLE_DEV_TOKENIZER).
# ============================================================================

"""Mock card tokenizer — pure functions + MockTokenizerAdapter wrapper."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

from bss_clock import now as clock_now

from app.domain.tokenizer import ChargeResult, TokenizerAdapter, TokenizeResult


def _detect_brand(card_number: str) -> str:
    """BIN-based brand detection."""
    digits = card_number.replace(" ", "").replace("-", "")
    if not digits:
        return "unknown"
    if digits[0] == "4":
        return "visa"
    if len(digits) >= 2 and digits[:2] in ("34", "37"):
        return "amex"
    if len(digits) >= 2 and 51 <= int(digits[:2]) <= 55:
        return "mastercard"
    return "unknown"


def tokenize_card(
    card_number: str, exp_month: int, exp_year: int, cvv: str
) -> TokenizeResult:
    """Tokenize a card number into a mock token.

    Pure function — no DB, no HTTP.
    - BIN-based brand detection (4xxx=visa, 51-55=mc, 34/37=amex)
    - card_number containing "FAIL" or "DECLINE" embeds that in the token
    - Expired card raises ValueError
    """
    now = clock_now()
    if exp_year < now.year or (exp_year == now.year and exp_month < now.month):
        raise ValueError(
            f"Card expired: {exp_month:02d}/{exp_year}"
        )

    digits = card_number.replace(" ", "").replace("-", "")
    last4 = digits[-4:] if len(digits) >= 4 else digits
    brand = _detect_brand(digits)

    # Special-case: embed FAIL/DECLINE in the token so charge() knows to decline
    uid = str(uuid4())
    if "FAIL" in card_number.upper():
        token = f"tok_FAIL_{uid}"
    elif "DECLINE" in card_number.upper():
        token = f"tok_DECLINE_{uid}"
    else:
        token = f"tok_{uid}"

    return TokenizeResult(token=token, last4=last4, brand=brand)


async def charge(token: str, amount: Decimal, currency: str) -> ChargeResult:
    """Simulate a gateway charge.

    Pure function with simulated latency — no DB, no HTTP.
    - token containing "FAIL" or "DECLINE" → declined
    - amount <= 0 → ValueError
    - else → approved with mock gateway ref

    The v0.16 ``provider_call_id`` and ``decline_code`` fields are
    populated for ChargeResult shape consistency with Stripe; mock uses
    the gateway_ref as the provider_call_id (it's the only id mock has)
    and a coarse decline_code on declines.
    """
    await asyncio.sleep(0.05)  # simulate gateway latency

    if amount <= 0:
        raise ValueError("amount must be positive")

    gateway_ref = f"mock_{uuid4()}"

    if "FAIL" in token or "DECLINE" in token:
        return ChargeResult(
            status="declined",
            reason="card_declined_by_issuer",
            gateway_ref=gateway_ref,
            provider_call_id=gateway_ref,
            decline_code="card_declined",
        )

    return ChargeResult(
        status="approved",
        reason=None,
        gateway_ref=gateway_ref,
        provider_call_id=gateway_ref,
        decline_code=None,
    )


# ── MockTokenizerAdapter (v0.16) ────────────────────────────────────────
#
# Wraps the module-level functions in a class implementing TokenizerAdapter
# so PaymentService can inject the adapter via constructor (one shared
# interface across mock + Stripe). The module-level functions stay
# (used by the dev `bss payment add-card` CLI and existing tests) so
# nothing downstream of those breaks.


class MockTokenizerAdapter:
    """In-process mock — no DB, no HTTP, no provider account.

    Preserves the v0.1 ``tok_FAIL_*`` / ``tok_DECLINE_*`` test
    affordances exactly: hero scenarios that test failed-charge paths
    keep working when ``BSS_PAYMENT_PROVIDER=mock``.

    ``ensure_customer`` returns a deterministic ``cus_mock_<bss_id>``
    so multiple charges for the same BSS customer get the same
    provider-side ref — matches Stripe's own idempotency behavior.
    ``attach_payment_method_to_customer`` is a no-op (mock tokens
    aren't bound to provider customers; mock charges accept any
    token + customer pair).
    """

    async def charge(
        self,
        token: str,
        amount: Decimal,
        currency: str,
        *,
        idempotency_key: str,
        purpose: str,
        customer_external_ref: str | None,
    ) -> ChargeResult:
        # idempotency_key is observed but not used — mock has no
        # provider-side dedupe to honor; the BSS payment_attempt row's
        # idempotency_key column is the source of truth in mock mode.
        return await charge(token, amount, currency)

    async def tokenize(
        self,
        card_number: str,
        exp_month: int,
        exp_year: int,
        cvv: str,
    ) -> TokenizeResult:
        return tokenize_card(card_number, exp_month, exp_year, cvv)

    async def attach_payment_method_to_customer(
        self,
        *,
        payment_method_id: str,
        customer_id: str,
    ) -> None:
        return None

    async def ensure_customer(
        self,
        *,
        bss_customer_id: str,
        email: str,
    ) -> str:
        return f"cus_mock_{bss_customer_id}"


# Compile-time check that MockTokenizerAdapter satisfies the Protocol.
_protocol_witness: TokenizerAdapter = MockTokenizerAdapter()
