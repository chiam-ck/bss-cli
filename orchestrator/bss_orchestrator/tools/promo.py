"""Promotion tools — operator/admin promo management (v1.1).

These compose over loyalty-cli through the catalog service. Operator-only by
doctrine: ``promo.create`` / ``promo.assign`` are in the ``operator_cockpit``
(and ``default``) profiles, NEVER ``customer_self_serve`` — a customer can
*type* a code at checkout but cannot enumerate or self-issue promotions.
``order.create`` carries the typed-code path for customers.
"""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import (
    CustomerId,
    DiscountType,
    DurationKind,
    ProductOfferingId,
    PromoAudience,
    PromoCodeKind,
    PromotionId,
)
from ._registry import register


@register("promo.create")
async def promo_create(
    promotion_id: PromotionId,
    discount_type: DiscountType,
    discount_value: str,
    duration_kind: DurationKind,
    audience: PromoAudience = "public",
    currency: str = "SGD",
    code: str | None = None,
    promo_code_kind: PromoCodeKind | None = None,
    applicable_offering_ids: list[ProductOfferingId] | None = None,
    periods_total: int | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Create a promotion (two-system saga: BSS money terms + loyalty code).

    Operator/admin only. Both audiences register a real loyalty code.

    Args:
        promotion_id: Stable id, e.g. ``PROMO_SUMMER25``. Used as the loyalty
            idempotency key; a retry resumes a half-finished saga.
        discount_type: ``percent`` or ``absolute``.
        discount_value: Amount as a string (Decimal-safe). Percent must be 0-100.
        duration_kind: ``single`` (activation period only), ``multi`` (N periods,
            requires ``periods_total`` >= 2), or ``perpetual`` (never reverts).
        audience: ``public`` (advertised; anyone may type the code) or
            ``targeted`` (not advertised; auto-applies only for customers added
            via ``promo.assign``; a code is derived from the id if omitted).
        currency: ISO-4217 for absolute discounts. Default SGD.
        code: The promo code. Required for ``public``; derived from the id for
            ``targeted`` if omitted.
        promo_code_kind: ``single_use_shared`` | ``multi_use`` |
            ``single_use_unique_per_customer``. Defaults to one-per-customer for
            targeted.
        applicable_offering_ids: Restrict to these plans. Omit = all sellable.
        periods_total: Required for ``multi`` (>= 2); omit otherwise.
        valid_from / valid_to: Optional ISO-8601 validity window.
        display_name: Optional human label (defaults to the promotion id).

    Returns:
        The promotion dict with ``state="active"`` and ``offerDefinitionId`` set.

    Raises:
        PolicyViolationFromServer:
            - ``catalog.promotion.already_exists`` / ``code_in_use``
            - ``catalog.promotion.invalid_*`` / ``requires_code``
            - ``catalog.promotion.loyalty_refused`` (translated loyalty refusal)
    """
    return await get_clients().catalog.create_promotion(
        promotion_id=promotion_id,
        discount_type=discount_type,
        discount_value=discount_value,
        duration_kind=duration_kind,
        audience=audience,
        currency=currency,
        code=code,
        promo_code_kind=promo_code_kind,
        applicable_offering_ids=applicable_offering_ids,
        periods_total=periods_total,
        valid_from=valid_from,
        valid_to=valid_to,
        display_name=display_name,
    )


@register("promo.assign")
async def promo_assign(
    promotion_id: PromotionId,
    customer_ids: list[CustomerId],
) -> dict[str, Any]:
    """Add customers to a *targeted* promotion's eligibility list.

    Operator/admin only. The promotion's code then auto-applies for these
    customers at their next order (and a typed attempt by them validates).
    Re-runnable — a customer already eligible is reported under ``already``.

    Args:
        promotion_id: An ``active`` ``targeted`` promotion id (from ``promo.create
            audience=targeted``).
        customer_ids: The chosen audience (CUST- prefixed).

    Returns:
        ``{promotionId, code, eligible: [...], already: [...]}``.

    Raises:
        PolicyViolationFromServer:
            - ``catalog.promotion.not_targeted``: missing, inactive, or public.
    """
    return await get_clients().catalog.assign_promotion(
        promotion_id, customer_ids=customer_ids
    )


@register("promo.show")
async def promo_show(promotion_id: PromotionId) -> dict[str, Any]:
    """Read a promotion's money terms + loyalty link + state.

    Args:
        promotion_id: The promotion id.

    Returns:
        The promotion dict (discount terms, code, offerDefinitionId, state).

    Raises:
        NotFound: no such promotion.
    """
    return await get_clients().catalog.get_promotion(promotion_id)
