"""Catalog tools — TMF620 product offerings + VAS."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..clients import get_clients
from ..types import (
    IsoDatetime,
    ProductOfferingId,
    ProductOfferingPriceId,
    VasOfferingId,
)
from ._registry import register


@register("catalog.list_offerings")
async def catalog_list_offerings() -> list[dict[str, Any]]:
    """List all product offerings (plans + VAS). In v0.1 there are exactly
    three plans — ``PLAN_S``, ``PLAN_M``, ``PLAN_L`` — plus a handful of VAS.

    Args:
        (none)

    Returns:
        List of offering dicts ``{id, name, price, allowances, type}``.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().catalog.list_offerings()


@register("catalog.get_offering")
async def catalog_get_offering(offering_id: ProductOfferingId) -> dict[str, Any]:
    """Get a single product offering with prices and allowances.

    Args:
        offering_id: Any active sellable plan ID — get the current set
            from ``catalog.list_active_offerings``. Unknown IDs come
            back as not found.

    Returns:
        Offering dict.

    Raises:
        NotFound: unknown offering ID.
    """
    return await get_clients().catalog.get_offering(offering_id)


@register("catalog.list_vas")
async def catalog_list_vas() -> list[dict[str, Any]]:
    """List all VAS (Value-Added Service) offerings — data top-ups, day passes, etc.
    Call this when the user asks to top up a blocked subscription so you can
    pick the right ``vasOfferingId`` for ``subscription.purchase_vas``.

    Args:
        (none)

    Returns:
        List of VAS dicts ``{id, name, price, bundleDelta}``.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().catalog.list_vas()


@register("catalog.get_vas")
async def catalog_get_vas(vas_offering_id: VasOfferingId) -> dict[str, Any]:
    """Get a single VAS offering.

    Args:
        vas_offering_id: VAS offering ID, e.g. ``VAS_DATA_5GB``, ``VAS_DATA_DAYPASS``.

    Returns:
        VAS offering dict.

    Raises:
        NotFound: unknown VAS offering.
    """
    return await get_clients().catalog.get_vas(vas_offering_id)


# ── v0.7 — active-aware reads ────────────────────────────────────────


@register("catalog.list_active_offerings")
async def catalog_list_active_offerings(
    at: IsoDatetime | None = None,
) -> list[dict[str, Any]]:
    """List offerings sellable at a given moment, sorted by lowest active price.

    Args:
        at: Optional ISO-8601 instant. Defaults to now via ``bss_clock``.

    Returns:
        List of offering dicts that pass time-bound + lifecycle + sellable filters.

    Raises:
        (none expected — read tool)
    """
    moment = datetime.fromisoformat(at) if at else None
    return await get_clients().catalog.list_active_offerings(at=moment)


@register("catalog.get_active_price")
async def catalog_get_active_price(
    offering_id: ProductOfferingId,
    at: IsoDatetime | None = None,
) -> dict[str, Any]:
    """Resolve the active recurring price row for an offering at a moment.

    When multiple rows are simultaneously active (base + windowed promo),
    the lowest amount wins.

    Args:
        offering_id: ``PLAN_S`` / ``PLAN_M`` / ``PLAN_L`` (or a v0.7+ added plan).
        at: Optional ISO-8601 instant. Defaults to now.

    Returns:
        TMF620 price dict.

    Raises:
        PolicyViolationFromServer:
            - ``catalog.price.no_active_row``: nothing is active for that
              offering at the requested moment.
    """
    moment = datetime.fromisoformat(at) if at else None
    return await get_clients().catalog.get_active_price(offering_id, at=moment)


# ── v0.7 — admin write paths (CLI + scenarios only; hidden from LLM) ──


@register("catalog.add_offering")
async def catalog_add_offering(
    offering_id: ProductOfferingId,
    name: str,
    amount: str,
    currency: str = "SGD",
    valid_from: IsoDatetime | None = None,
    valid_to: IsoDatetime | None = None,
    data_mb: int | None = None,
    voice_minutes: int | None = None,
    sms_count: int | None = None,
    data_roaming_mb: int | None = None,
) -> dict[str, Any]:
    """Admin: add a new product offering with price + allowances. Hidden from LLM.

    Args:
        offering_id: Plan ID (e.g. ``PLAN_XS``).
        name: Display name.
        amount: Recurring price amount as a string (Decimal-safe).
        currency: ISO-4217. Default SGD.
        valid_from: Optional ISO-8601; NULL = always-active.
        valid_to: Optional ISO-8601; NULL = no end (exclusive boundary).
        data_mb: Optional data allowance.
        voice_minutes: Optional voice allowance.
        sms_count: Optional SMS allowance.
        data_roaming_mb: Optional roaming-data allowance (v0.20+). 0 is
            permitted (plan with no included roaming, customer can still
            top up via VAS_ROAMING_*).

    Returns:
        TMF620 offering dict.

    Raises:
        PolicyViolationFromServer:
            - ``catalog.offering.already_exists``: id already in use.
    """
    return await get_clients().catalog.admin_add_offering(
        offering_id=offering_id,
        name=name,
        amount=amount,
        currency=currency,
        valid_from=datetime.fromisoformat(valid_from) if valid_from else None,
        valid_to=datetime.fromisoformat(valid_to) if valid_to else None,
        data_mb=data_mb,
        voice_minutes=voice_minutes,
        sms_count=sms_count,
        data_roaming_mb=data_roaming_mb,
    )


@register("catalog.add_price")
async def catalog_add_price(
    offering_id: ProductOfferingId,
    price_id: ProductOfferingPriceId,
    amount: str,
    currency: str = "SGD",
    valid_from: IsoDatetime | None = None,
    valid_to: IsoDatetime | None = None,
    retire_current: bool = False,
) -> dict[str, Any]:
    """Admin: insert a new product_offering_price row. Hidden from LLM.

    Args:
        offering_id: The offering to attach the new price to.
        price_id: Caller-supplied unique id for the new row.
        amount: Recurring price amount as a string.
        currency: ISO-4217.
        valid_from: Optional ISO-8601. NULL = active immediately.
        valid_to: Optional ISO-8601. NULL = no end.
        retire_current: If True, every existing open-ended row on the
            offering gets stamped with valid_to so the new row takes over.

    Returns:
        TMF620 price dict.

    Raises:
        PolicyViolationFromServer:
            - ``catalog.offering.not_found``.
            - ``catalog.price.already_exists``.
    """
    return await get_clients().catalog.admin_add_price(
        offering_id,
        price_id=price_id,
        amount=amount,
        currency=currency,
        valid_from=datetime.fromisoformat(valid_from) if valid_from else None,
        valid_to=datetime.fromisoformat(valid_to) if valid_to else None,
        retire_current=retire_current,
    )


@register("catalog.window_offering")
async def catalog_window_offering(
    offering_id: ProductOfferingId,
    valid_from: IsoDatetime | None = None,
    valid_to: IsoDatetime | None = None,
) -> dict[str, Any]:
    """Admin: set valid_from / valid_to on an existing offering. Hidden from LLM.

    Args:
        offering_id: The offering to time-bound.
        valid_from: Optional ISO-8601 (NULL clears).
        valid_to: Optional ISO-8601 (NULL clears).

    Returns:
        Updated TMF620 offering dict.

    Raises:
        PolicyViolationFromServer:
            - ``catalog.offering.not_found``.
    """
    return await get_clients().catalog.admin_set_offering_window(
        offering_id,
        valid_from=datetime.fromisoformat(valid_from) if valid_from else None,
        valid_to=datetime.fromisoformat(valid_to) if valid_to else None,
    )
