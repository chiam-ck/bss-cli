"""Subscription Pydantic schemas — camelCase for API."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from bss_models import apply_discount
from bss_models.subscription import BundleBalance, Subscription, VasPurchase

SUBSCRIPTION_PATH = "/subscription-api/v1/subscription"


class PriceSnapshot(BaseModel):
    """Price row captured at order-creation time, persisted for renewal."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    price_amount: Decimal
    price_currency: str
    price_offering_price_id: str
    # v1.1 — optional promo discount carried from the order. price_amount stays
    # the FULL base; the effective per-period charge is computed at charge time.
    discount_type: str | None = None
    discount_value: Decimal | None = None
    discount_periods_total: int | None = None
    promo_code: str | None = None
    promo_offer_definition_id: str | None = None


class SubscriptionCreateRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    customer_id: str
    offering_id: str
    msisdn: str
    iccid: str
    payment_method_id: str
    # v0.7 — optional during the COM/SOM rollout. When omitted, the service
    # falls back to the catalog's recurring price (legacy path).
    price_snapshot: PriceSnapshot | None = None


class VasPurchaseRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    vas_offering_id: str


class BundleBalanceResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    subscription_id: str
    allowance_type: str
    total: int
    consumed: int
    remaining: int
    unit: str
    period_start: datetime | None = None
    period_end: datetime | None = None


class VasPurchaseResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    subscription_id: str
    vas_offering_id: str
    payment_attempt_id: str | None = None
    applied_at: datetime | None = None
    expires_at: datetime | None = None
    allowance_added: int
    allowance_type: str


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    href: str
    customer_id: str
    offering_id: str
    msisdn: str
    iccid: str
    cfs_service_id: str | None = None
    state: str
    state_reason: str | None = None
    activated_at: datetime | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    next_renewal_at: datetime | None = None
    terminated_at: datetime | None = None
    balances: list[BundleBalanceResponse] = []
    # v0.7 — price snapshot + pending plan-change fields.
    price_amount: Decimal | None = None
    price_currency: str | None = None
    price_offering_price_id: str | None = None
    pending_offering_id: str | None = None
    pending_offering_price_id: str | None = None
    pending_effective_at: datetime | None = None
    # v1.1 — promo discount snapshot (the portal dashboard renders these).
    # price_amount above is the FULL base; effective_amount is what's charged
    # this period. discount_periods_remaining: >0 discounted periods left,
    # 0 none, -1 perpetual.
    discount_type: str | None = None
    discount_value: Decimal | None = None
    discount_periods_remaining: int = 0
    effective_amount: Decimal | None = None
    promo_code: str | None = None
    promo_offer_definition_id: str | None = None
    at_type: str = "Subscription"


def to_balance_response(b: BundleBalance) -> BundleBalanceResponse:
    return BundleBalanceResponse(
        id=b.id,
        subscription_id=b.subscription_id,
        allowance_type=b.allowance_type,
        total=b.total,
        consumed=b.consumed,
        remaining=b.total - b.consumed if b.total >= 0 else -1,
        unit=b.unit,
        period_start=b.period_start,
        period_end=b.period_end,
    )


def to_subscription_response(sub: Subscription) -> SubscriptionResponse:
    return SubscriptionResponse(
        id=sub.id,
        href=f"{SUBSCRIPTION_PATH}/{sub.id}",
        customer_id=sub.customer_id,
        offering_id=sub.offering_id,
        msisdn=sub.msisdn,
        iccid=sub.iccid,
        cfs_service_id=sub.cfs_service_id,
        state=sub.state,
        state_reason=sub.state_reason,
        activated_at=sub.activated_at,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        next_renewal_at=sub.next_renewal_at,
        terminated_at=sub.terminated_at,
        balances=[to_balance_response(b) for b in sub.balances] if sub.balances else [],
        price_amount=sub.price_amount,
        price_currency=sub.price_currency,
        price_offering_price_id=sub.price_offering_price_id,
        pending_offering_id=sub.pending_offering_id,
        pending_offering_price_id=sub.pending_offering_price_id,
        pending_effective_at=sub.pending_effective_at,
        discount_type=sub.discount_type,
        discount_value=sub.discount_value,
        discount_periods_remaining=sub.discount_periods_remaining,
        # What this period actually charges: discounted while the counter is
        # live (>0 or perpetual -1), else the full base.
        effective_amount=(
            apply_discount(sub.discount_type, sub.discount_value, sub.price_amount)
            if sub.discount_type
            and sub.discount_value is not None
            and sub.discount_periods_remaining != 0
            else sub.price_amount
        ),
        promo_code=sub.promo_code,
        promo_offer_definition_id=sub.promo_offer_definition_id,
    )


def to_vas_purchase_response(vp: VasPurchase) -> VasPurchaseResponse:
    return VasPurchaseResponse(
        id=vp.id,
        subscription_id=vp.subscription_id,
        vas_offering_id=vp.vas_offering_id,
        payment_attempt_id=vp.payment_attempt_id,
        applied_at=vp.applied_at,
        expires_at=vp.expires_at,
        allowance_added=vp.allowance_added,
        allowance_type=vp.allowance_type,
    )
