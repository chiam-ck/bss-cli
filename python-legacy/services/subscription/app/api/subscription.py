"""Subscription API routers — no business logic."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.dependencies import get_subscription_service
from app.schemas.subscription import (
    BundleBalanceResponse,
    SubscriptionCreateRequest,
    SubscriptionResponse,
    VasPurchaseRequest,
    to_balance_response,
    to_subscription_response,
)
from app.services.subscription_service import SubscriptionService

router = APIRouter(tags=["Subscription"])


@router.post("/subscription", response_model=SubscriptionResponse, status_code=201)
async def create_subscription(
    body: SubscriptionCreateRequest,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionResponse:
    sub = await svc.create(
        customer_id=body.customer_id,
        offering_id=body.offering_id,
        msisdn=body.msisdn,
        iccid=body.iccid,
        payment_method_id=body.payment_method_id,
        price_snapshot=(
            body.price_snapshot.model_dump(by_alias=True) if body.price_snapshot else None
        ),
        commercial_order_id=body.commercial_order_id,
    )
    return to_subscription_response(sub)


@router.get("/subscription/{sub_id}", response_model=SubscriptionResponse)
async def get_subscription(
    sub_id: str,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionResponse:
    sub = await svc.get(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail=f"Subscription {sub_id} not found")
    return to_subscription_response(sub)


@router.get("/subscription", response_model=list[SubscriptionResponse])
async def list_subscriptions(
    customerId: str | None = None,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> list[SubscriptionResponse]:
    if not customerId:
        raise HTTPException(status_code=400, detail="customerId query param is required")
    subs = await svc.list_for_customer(customerId)
    return [to_subscription_response(s) for s in subs]


@router.get("/subscription/by-msisdn/{msisdn}", response_model=SubscriptionResponse)
async def get_by_msisdn(
    msisdn: str,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionResponse:
    sub = await svc.get_by_msisdn(msisdn)
    if not sub:
        raise HTTPException(status_code=404, detail=f"No subscription for MSISDN {msisdn}")
    return to_subscription_response(sub)


@router.get("/subscription/{sub_id}/balance", response_model=list[BundleBalanceResponse])
async def get_balance(
    sub_id: str,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> list[BundleBalanceResponse]:
    balances = await svc.get_balances(sub_id)
    if not balances:
        raise HTTPException(status_code=404, detail=f"No balances for {sub_id}")
    return [to_balance_response(b) for b in balances]


@router.post("/subscription/{sub_id}/vas-purchase", response_model=SubscriptionResponse)
async def purchase_vas(
    sub_id: str,
    body: VasPurchaseRequest,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionResponse:
    sub = await svc.purchase_vas(sub_id, body.vas_offering_id)
    return to_subscription_response(sub)


@router.post("/subscription/{sub_id}/renew", response_model=SubscriptionResponse)
async def renew_subscription(
    sub_id: str,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionResponse:
    sub = await svc.renew(sub_id)
    return to_subscription_response(sub)


class TerminateRequest(BaseModel):
    """Optional body for /terminate. Empty body still works (back-compat).

    v0.17 — ``release_inventory`` defaults True. Set False from the MNP
    port-out flow, where the donor MSISDN has already been flipped to
    terminal ``ported_out`` upstream.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    reason: str | None = None
    release_inventory: bool = True


@router.post("/subscription/{sub_id}/terminate", response_model=SubscriptionResponse)
async def terminate_subscription(
    sub_id: str,
    body: TerminateRequest | None = None,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionResponse:
    reason = (body.reason if body is not None else None) or "customer_requested"
    release_inventory = body.release_inventory if body is not None else True
    sub = await svc.terminate(
        sub_id, reason=reason, release_inventory=release_inventory
    )
    return to_subscription_response(sub)


# ── v0.7 — plan change ───────────────────────────────────────────────


class SchedulePlanChangeRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    new_offering_id: str


@router.post(
    "/subscription/{sub_id}/schedule-plan-change",
    response_model=SubscriptionResponse,
)
async def schedule_plan_change(
    sub_id: str,
    body: SchedulePlanChangeRequest,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionResponse:
    sub = await svc.schedule_plan_change(sub_id, body.new_offering_id)
    return to_subscription_response(sub)


@router.post(
    "/subscription/{sub_id}/cancel-plan-change",
    response_model=SubscriptionResponse,
)
async def cancel_plan_change(
    sub_id: str,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> SubscriptionResponse:
    sub = await svc.cancel_pending_plan_change(sub_id)
    return to_subscription_response(sub)


# ── v0.7 — operator price migration ─────────────────────────────────


from datetime import datetime


class MigratePriceRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    offering_id: str
    new_price_id: str
    effective_from: datetime
    notice_days: int = 30
    initiated_by: str


class MigratePriceResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    count: int
    subscription_ids: list[str]


@router.post(
    "/admin/subscription/migrate-price",
    response_model=MigratePriceResponse,
)
async def migrate_price(
    body: MigratePriceRequest,
    svc: SubscriptionService = Depends(get_subscription_service),
) -> MigratePriceResponse:
    """Operator-initiated price migration with notice. Admin-only."""
    result = await svc.migrate_subscriptions_to_price(
        filter={"offering_id": body.offering_id},
        new_price_id=body.new_price_id,
        effective_from=body.effective_from,
        notice_days=body.notice_days,
        initiated_by=body.initiated_by,
    )
    return MigratePriceResponse(
        count=result["count"],
        subscription_ids=result["subscriptionIds"],
    )
