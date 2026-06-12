"""Payment method policies — 5 rules.

payment_method.add.customer_exists          — cross-service via CRMClient
payment_method.add.customer_active_or_pending — cross-service, checks status
payment_method.add.card_not_expired         — local validation
payment_method.add.at_most_n_methods        — queries repo
payment_method.remove.not_last_if_active_subscription — STUB (Phase 6)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bss_clients import CRMClient, NotFound
from bss_clock import now as clock_now

from .base import PolicyViolation, policy

if TYPE_CHECKING:
    from app.repositories.payment_method_repo import PaymentMethodRepository

MAX_METHODS_PER_CUSTOMER = 5


@policy("payment_method.add.customer_exists")
async def check_customer_exists(
    customer_id: str, crm_client: CRMClient
) -> dict:
    """Returns the CRM customer payload for downstream policy use."""
    try:
        return await crm_client.get_customer(customer_id)
    except NotFound:
        raise PolicyViolation(
            rule="payment_method.add.customer_exists",
            message=f"Customer {customer_id} does not exist",
            context={"customer_id": customer_id},
        )


@policy("payment_method.add.customer_active_or_pending")
def check_customer_active_or_pending(customer: dict) -> None:
    """Customer status must be active or pending."""
    status = customer.get("status", "")
    if status not in ("active", "pending"):
        raise PolicyViolation(
            rule="payment_method.add.customer_active_or_pending",
            message=f"Customer status is '{status}', must be active or pending",
            context={"status": status},
        )


@policy("payment_method.add.card_not_expired")
def check_card_not_expired(exp_month: int, exp_year: int) -> None:
    """Card must not be expired."""
    now = clock_now()
    if exp_year < now.year or (exp_year == now.year and exp_month < now.month):
        raise PolicyViolation(
            rule="payment_method.add.card_not_expired",
            message=f"Card expired: {exp_month:02d}/{exp_year}",
            context={"exp_month": exp_month, "exp_year": exp_year},
        )


@policy("payment_method.add.at_most_n_methods")
async def check_at_most_n_methods(
    customer_id: str, repo: PaymentMethodRepository
) -> None:
    """Customer cannot have more than MAX_METHODS_PER_CUSTOMER active methods."""
    count = await repo.count_active_for_customer(customer_id)
    if count >= MAX_METHODS_PER_CUSTOMER:
        raise PolicyViolation(
            rule="payment_method.add.at_most_n_methods",
            message=(
                f"Customer {customer_id} already has {count} active payment "
                f"methods (max {MAX_METHODS_PER_CUSTOMER})"
            ),
            context={
                "customer_id": customer_id,
                "current_count": count,
                "max": MAX_METHODS_PER_CUSTOMER,
            },
        )


@policy("payment_method.remove.not_last_if_active_subscription")
async def check_not_last_if_active_subscription(
    customer_id: str, repo: PaymentMethodRepository
) -> None:
    """STUB — Phase 5.

    In Phase 6 this will call SubscriptionClient.list_for_customer() to check
    for active subscriptions. For now, it only blocks removal if the customer
    has exactly 1 active method (the one being removed), under the assumption
    that the real check will be wired when SubscriptionClient exists.

    Phase 5 behavior: always allows removal (stub).
    Phase 6 behavior: blocks if active_methods == 1 AND active subscriptions > 0.
    """
    # STUB: real cross-service check added in Phase 6
    pass
