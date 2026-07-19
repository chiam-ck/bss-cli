"""Pure rating function — no DB, no HTTP, no side effects.

Doctrine: bundled prepaid only. Rating is a unit-conversion +
allowance-mapping function. charge_amount is always 0 — the customer paid
for the bundle upfront. consumed_quantity == usage quantity (no tiering,
no time-of-day rules, no per-unit charging).

Rating remains a separate service so future rating rules can live here
without touching Subscription or Mediation.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# event_type → allowance_type mapping. The event types are defined in the
# mediation VALID_EVENT_TYPES set; both `voice` and `voice_minutes` map to
# the voice allowance for legacy/alias reasons.
EVENT_TYPE_TO_ALLOWANCE: dict[str, str] = {
    "data": "data",
    "voice": "voice",
    "voice_minutes": "voice",
    "sms": "sms",
}

# allowance_type → canonical unit. Used to validate the usage event's unit
# matches the tariff's allowance unit. -1 quantity in a tariff means unlimited.
ALLOWANCE_UNIT: dict[str, str] = {
    "data": "mb",
    "voice": "minutes",
    "sms": "count",
}


class RatingError(Exception):
    """Raised when a usage event cannot be rated against the given tariff."""


@dataclass(frozen=True)
class UsageInput:
    """Subset of a UsageEvent needed for rating."""

    usage_event_id: str
    subscription_id: str
    msisdn: str
    event_type: str
    quantity: int
    unit: str


@dataclass(frozen=True)
class RatingResult:
    """Output of `rate_usage` — the decrement instruction for Subscription."""

    usage_event_id: str
    subscription_id: str
    allowance_type: str
    consumed_quantity: int
    unit: str
    charge_amount: Decimal  # always 0 for bundled prepaid v0.1
    currency: str


def rate_usage(
    usage: UsageInput,
    tariff: dict[str, Any],
) -> RatingResult:
    """Pure function. Returns the decrement instruction for Subscription.

    Args:
        usage: the incoming usage event (already validated by Mediation).
        tariff: offering document with `bundleAllowance` — the list of
            allowances included in this bundle. Shape matches Catalog's
            `productOffering` response.

    Raises:
        RatingError: if the event_type isn't mapped to any allowance, if
            the tariff doesn't include that allowance type, or if the
            usage unit doesn't match the tariff's allowance unit.

    For bundled prepaid:
        charge_amount = 0
        consumed_quantity = usage.quantity  (no tiering, no TOD rules)
    """
    allowance_type = EVENT_TYPE_TO_ALLOWANCE.get(usage.event_type)
    if allowance_type is None:
        raise RatingError(
            f"No allowance mapping for event_type '{usage.event_type}'"
        )

    allowances = tariff.get("bundleAllowance") or []
    matching = next(
        (a for a in allowances if a.get("allowanceType") == allowance_type),
        None,
    )
    if matching is None:
        raise RatingError(
            f"Tariff '{tariff.get('id')}' has no '{allowance_type}' allowance"
        )

    expected_unit = ALLOWANCE_UNIT[allowance_type]
    if usage.unit != expected_unit:
        raise RatingError(
            f"Usage unit '{usage.unit}' does not match allowance unit "
            f"'{expected_unit}' for {allowance_type}"
        )

    currency = "SGD"
    price_entries = tariff.get("productOfferingPrice") or []
    for p in price_entries:
        amount = (
            (p.get("price") or {})
            .get("taxIncludedAmount", {})
            .get("unit")
        )
        if amount:
            currency = amount
            break

    return RatingResult(
        usage_event_id=usage.usage_event_id,
        subscription_id=usage.subscription_id,
        allowance_type=allowance_type,
        consumed_quantity=usage.quantity,
        unit=usage.unit,
        charge_amount=Decimal("0"),
        currency=currency,
    )
