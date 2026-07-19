"""Payment charge policies — 4 rules.

payment.charge.method_active
payment.charge.positive_amount
payment.charge.customer_matches_method
payment.charge.token_provider_matches_active   (v0.16+)
"""

from __future__ import annotations

from decimal import Decimal

from bss_models import PaymentMethod

from .base import PolicyViolation, policy


@policy("payment.charge.method_active")
def check_method_active(method: PaymentMethod) -> None:
    """Payment method must be active (not removed)."""
    if method.status != "active":
        raise PolicyViolation(
            rule="payment.charge.method_active",
            message=f"Payment method {method.id} status is '{method.status}', must be active",
            context={"payment_method_id": method.id, "status": method.status},
        )


@policy("payment.charge.positive_amount")
def check_positive_amount(amount: Decimal) -> None:
    """Charge amount must be positive."""
    if amount <= 0:
        raise PolicyViolation(
            rule="payment.charge.positive_amount",
            message=f"Charge amount must be positive, got {amount}",
            context={"amount": str(amount)},
        )


@policy("payment.charge.customer_matches_method")
def check_customer_matches_method(
    customer_id: str, method: PaymentMethod
) -> None:
    """Customer ID on charge request must match the method's customer_id."""
    if method.customer_id != customer_id:
        raise PolicyViolation(
            rule="payment.charge.customer_matches_method",
            message=f"Payment method {method.id} belongs to {method.customer_id}, not {customer_id}",
            context={
                "requested_customer_id": customer_id,
                "method_customer_id": method.customer_id,
                "payment_method_id": method.id,
            },
        )


# Mapping from adapter class name → token_provider value the active
# adapter expects. Lazy-fail cutover (v0.16) checks the row's
# token_provider against this; mismatch raises with a structured rule
# so the customer's "add a new card" flow can recover cleanly.
_ADAPTER_EXPECTS = {
    "MockTokenizerAdapter": "mock",
    "StripeTokenizerAdapter": "stripe",
}


@policy("payment.charge.token_provider_matches_active")
def check_token_provider_matches_active(
    method: PaymentMethod, adapter_class_name: str
) -> None:
    """Cutover doctrine — token_provider on the row must match the active adapter.

    A ``payment_method.token`` minted under ``BSS_PAYMENT_PROVIDER=mock``
    (``tok_<uuid>``) is unusable when the active adapter is Stripe (and
    vice versa). This is the lazy-fail half of the v0.16 cutover
    contract: charges error cleanly so the portal's "add a new card"
    flow recovers; the proactive half is ``bss payment cutover``.

    The mismatch is intentionally non-recoverable at charge time —
    silently reissuing a Stripe ``pm_*`` from a mock ``tok_*`` would
    require the customer to re-enter their card via Elements anyway,
    so we let the error propagate and the portal handles the re-add.
    """
    expected = _ADAPTER_EXPECTS.get(adapter_class_name)
    if expected is None:
        # Unknown adapter (probably a test double) — don't trip the
        # guard; the test is responsible for setting up matching
        # token_provider on its fixtures.
        return
    actual = getattr(method, "token_provider", None) or "mock"
    if actual != expected:
        raise PolicyViolation(
            rule="payment.charge.token_provider_matches_active",
            message=(
                f"Payment method {method.id} has token_provider={actual!r}, "
                f"but the active tokenizer is {adapter_class_name} "
                f"(expects token_provider={expected!r}). "
                "Customer must re-add their card; "
                "see `docs/runbooks/stripe-cutover.md`."
            ),
            context={
                "payment_method_id": method.id,
                "row_token_provider": actual,
                "active_adapter": adapter_class_name,
                "expected_token_provider": expected,
            },
        )
