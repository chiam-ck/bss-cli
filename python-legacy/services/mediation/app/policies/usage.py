"""Mediation policies — enforced BEFORE the usage_event row is persisted.

These are the block-at-edge doctrine. A blocked subscription, unknown MSISDN,
or malformed quantity is rejected with 422 — no CDR is recorded.
"""

from typing import Any

from bss_clients import SubscriptionClient
from bss_clients.errors import NotFound

from app.policies.base import PolicyViolation, policy

VALID_EVENT_TYPES = frozenset({"data", "voice", "voice_minutes", "sms"})


@policy("usage.record.positive_quantity")
def check_positive_quantity(quantity: int) -> None:
    if quantity <= 0:
        raise PolicyViolation(
            rule="usage.record.positive_quantity",
            message=f"Quantity must be positive, got {quantity}",
            context={"quantity": quantity},
        )


@policy("usage.record.valid_event_type")
def check_valid_event_type(event_type: str) -> None:
    if event_type not in VALID_EVENT_TYPES:
        raise PolicyViolation(
            rule="usage.record.valid_event_type",
            message=f"Invalid event type '{event_type}'",
            context={
                "event_type": event_type,
                "valid_types": sorted(VALID_EVENT_TYPES),
            },
        )


@policy("usage.record.subscription_must_exist")
async def check_subscription_exists(
    msisdn: str, subscription_client: SubscriptionClient
) -> dict[str, Any]:
    """Must return the enriched subscription. NotFound → 422."""
    try:
        sub = await subscription_client.get_by_msisdn(msisdn)
    except NotFound:
        raise PolicyViolation(
            rule="usage.record.subscription_must_exist",
            message=f"No subscription for MSISDN {msisdn}",
            context={"msisdn": msisdn},
        )
    return sub


@policy("usage.record.msisdn_belongs_to_subscription")
def check_msisdn_matches(subscription: dict[str, Any], msisdn: str) -> None:
    """Defensive: enriched sub's MSISDN must equal the ingress MSISDN."""
    sub_msisdn = subscription.get("msisdn")
    if sub_msisdn != msisdn:
        raise PolicyViolation(
            rule="usage.record.msisdn_belongs_to_subscription",
            message=(
                f"Enriched subscription MSISDN '{sub_msisdn}' does not match "
                f"request MSISDN '{msisdn}'"
            ),
            context={
                "request_msisdn": msisdn,
                "subscription_msisdn": sub_msisdn,
                "subscription_id": subscription.get("id"),
            },
        )


@policy("usage.record.subscription_must_be_active")
def check_subscription_active(subscription: dict[str, Any]) -> None:
    """Block-at-edge: no usage recorded for non-active subscriptions."""
    state = subscription.get("state")
    if state != "active":
        raise PolicyViolation(
            rule="usage.record.subscription_must_be_active",
            message=f"Subscription {subscription.get('id')} is {state}, not active",
            context={
                "subscription_id": subscription.get("id"),
                "state": state,
            },
        )
