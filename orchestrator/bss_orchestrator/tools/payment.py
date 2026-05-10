"""Payment tools — methods (COF) + charge attempts (TMF676)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from ..clients import get_clients
from ..types import Currency, CustomerId, PaymentAttemptId, PaymentMethodId
from ._registry import register


def local_tokenize_card(card_number: str) -> dict[str, Any]:
    """Sandbox client-side tokenizer (mirrors services/payment mock_tokenizer).

    Payment service never exposed a public /dev/tokenize endpoint — real
    tokenization happens client-side (Stripe.js, etc.). We replicate the
    same FAIL/DECLINE embedding so charge-failure tests stay honest.
    """
    digits = card_number.replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) < 12:
        raise ValueError(f"Invalid card number: {card_number!r}")
    last4 = digits[-4:]
    bin6 = digits[:6]
    if digits[0] == "4":
        brand = "visa"
    elif 51 <= int(bin6[:2] or "0") <= 55:
        brand = "mastercard"
    elif bin6[:2] in ("34", "37"):
        brand = "amex"
    else:
        brand = "unknown"
    uid = str(uuid4())
    up = card_number.upper()
    if "FAIL" in up:
        token = f"tok_FAIL_{uid}"
    elif "DECLINE" in up:
        token = f"tok_DECLINE_{uid}"
    else:
        token = f"tok_{uid}"
    return {"cardToken": token, "last4": last4, "brand": brand}


@register("payment.add_card")
async def payment_add_card(
    customer_id: CustomerId,
    card_number: str,
) -> dict[str, Any]:
    """Tokenise a card PAN (dev tokenizer) and attach it as a payment method.
    In production, the PAN would be tokenised client-side (Stripe, etc.) —
    this tool is the sandbox convenience: tokenize + attach in one call.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        card_number: 16-digit PAN. Any number works in v0.1 UNLESS it
            contains ``FAIL`` or ``DECLINE`` (will be rejected by the mock).

    Returns:
        Payment method dict ``{id, customerId, brand, last4, expMonth, expYear, isDefault}``.

    Raises:
        PolicyViolationFromServer:
            - ``payment.add_card.customer_must_exist``: create the customer first.
            - ``payment.add_card.token_invalid``: card rejected by mock tokeniser.
    """
    c = get_clients()
    tok = local_tokenize_card(card_number)
    return await c.payment.create_payment_method(
        customer_id=customer_id,
        card_token=tok["cardToken"],
        last4=tok["last4"],
        brand=tok["brand"],
    )


@register("payment.list_methods")
async def payment_list_methods(customer_id: CustomerId) -> list[dict[str, Any]]:
    """List payment methods on file for a customer.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).

    Returns:
        List of payment method dicts.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().payment.list_methods(customer_id)


@register("payment.remove_method")
async def payment_remove_method(method_id: PaymentMethodId) -> dict[str, Any]:
    """Remove a payment method. DESTRUCTIVE — gated by ``safety.py``.
    Cannot remove the last active method if the customer has an active
    subscription (COF is mandatory policy).

    Args:
        method_id: Payment Method ID with the PM- prefix (opaque suffix).

    Returns:
        ``{"id": "PM-NNNN", "removed": true}``.

    Raises:
        PolicyViolationFromServer:
            - ``payment.remove_method.last_active_with_subscription``: add a
              replacement card first, or terminate the subscription.
    """
    return await get_clients().payment.remove_method(method_id)


@register("payment.charge")
async def payment_charge(
    customer_id: CustomerId,
    payment_method_id: PaymentMethodId,
    amount: str,
    purpose: str,
    currency: Currency = "SGD",
) -> dict[str, Any]:
    """Charge a customer's card-on-file. RARELY called by the LLM — order
    creation and VAS purchase charge COF internally. Only call this for
    exceptional manual charges.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        payment_method_id: Payment Method ID with the PM- prefix (opaque suffix).
        amount: Decimal amount as a string, e.g. ``"25.00"``. Avoid floats.
        purpose: Short reason, e.g. ``"manual_adjustment"``.
        currency: ISO-4217 currency code. v0.1 uses ``"SGD"`` only.

    Returns:
        Payment attempt dict ``{id, state, amount, currency, capturedAt}``.
        ``state`` is ``approved`` or ``declined``.

    Raises:
        PolicyViolationFromServer: various — read ``rule`` and retry or ask.
    """
    return await get_clients().payment.charge(
        customer_id=customer_id,
        payment_method_id=payment_method_id,
        amount=Decimal(amount),
        currency=currency,
        purpose=purpose,
    )


@register("payment.get_attempt")
async def payment_get_attempt(attempt_id: PaymentAttemptId) -> dict[str, Any]:
    """Read a single payment attempt by ID.

    Args:
        attempt_id: Payment Attempt ID with the PAY- prefix (opaque suffix).

    Returns:
        Payment attempt dict.

    Raises:
        NotFound: no attempt with this ID.
    """
    return await get_clients().payment.get_payment(attempt_id)


@register("payment.list_attempts")
async def payment_list_attempts(
    customer_id: CustomerId | None = None,
    payment_method_id: PaymentMethodId | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List payment attempts, optionally filtered.

    Args:
        customer_id: Filter by customer.
        payment_method_id: Filter by method.
        limit: Maximum rows (default 20).

    Returns:
        List of payment attempt dicts.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().payment.list_payments(
        customer_id=customer_id, payment_method_id=payment_method_id, limit=limit
    )
