"""Customer policies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.policies.base import PolicyViolation, policy
from app.repositories.customer_repo import CustomerRepository

if TYPE_CHECKING:
    from bss_clients.subscription import SubscriptionClient


@policy("customer.create.email_unique")
async def check_email_unique(email: str, repo: CustomerRepository) -> None:
    existing = await repo.find_by_email(email)
    if existing:
        raise PolicyViolation(
            rule="customer.create.email_unique",
            message=f"Email '{email}' is already registered",
            context={"email": email},
        )


@policy("customer.create.requires_contact_medium")
def check_requires_contact_medium(contact_mediums: list[dict]) -> None:
    if not contact_mediums:
        raise PolicyViolation(
            rule="customer.create.requires_contact_medium",
            message="At least one contact medium (email or phone) is required",
            context={},
        )


@policy("customer.close.no_active_subscriptions")
async def check_no_active_subscriptions(
    customer_id: str, subscription_client: SubscriptionClient
) -> None:
    subs = await subscription_client.list_for_customer(customer_id)
    active = [s for s in subs if s.get("state") in ("active", "pending")]
    if active:
        active_ids = [s["id"] for s in active]
        raise PolicyViolation(
            rule="customer.close.no_active_subscriptions",
            message=(
                f"Customer {customer_id} has {len(active)} active subscription(s): "
                f"{', '.join(active_ids)}. Terminate them first."
            ),
            context={"customer_id": customer_id, "active_subscriptions": active_ids},
        )
