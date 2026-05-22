"""Promotion routes — TMF671-shaped writes + portal-facing reads (v1.1).

Two surfaces on one router (mounted with no prefix, absolute paths):

* ``/tmf-api/promotionManagement/v4/promotion[...]`` — operator/admin writes
  and reads (create saga, get, list, targeted assign). `default` /
  `operator_cockpit` callers only; never customer-facing.
* ``/promo/preview`` + ``/promo/customer-offers`` — internal reads the
  self-serve portal proxies (the portal holds no loyalty token). ``customerId``
  is supplied by the portal from the authenticated session — catalog is behind
  the perimeter token, so this is the S2S contract, not a customer-trusted input.

PolicyViolation → 422 is handled by RequestIdMiddleware; not-found → 404 here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from bss_catalog.deps import get_promotion_service
from bss_catalog.promotion_service import PromotionService
from bss_models.catalog import Promotion

router = APIRouter(tags=["promotion"])

_TMF = "/tmf-api/promotionManagement/v4"


class _CamelBase(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ── request bodies ───────────────────────────────────────────────────────────


class CreatePromotionRequest(_CamelBase):
    promotion_id: str
    discount_type: str  # percent | absolute
    discount_value: Decimal
    duration_kind: str  # single | multi | perpetual
    audience: str = "public"  # public | targeted (v1.1.1)
    currency: str = "SGD"
    code: str | None = None  # derived from id for targeted if omitted
    promo_code_kind: str | None = None
    applicable_offering_ids: list[str] | None = None
    periods_total: int | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    display_name: str | None = None


class AssignTargetedRequest(_CamelBase):
    customer_ids: list[str]


# ── response models ──────────────────────────────────────────────────────────


class Tmf671Promotion(_CamelBase):
    id: str
    code: str | None
    name: str | None
    audience: str
    offer_definition_id: str | None
    discount_type: str
    discount_value: Decimal
    currency: str
    applicable_offering_ids: list[str] | None
    duration_kind: str
    periods_total: int | None
    valid_from: datetime | None
    valid_to: datetime | None
    state: str
    at_type: str = Field(default="Promotion", serialization_alias="@type")


def _to_tmf671(p: Promotion) -> Tmf671Promotion:
    return Tmf671Promotion(
        id=p.id,
        code=p.code,
        name=p.name,
        audience=p.audience,
        offer_definition_id=p.offer_definition_id,
        discount_type=p.discount_type,
        discount_value=p.discount_value,
        currency=p.currency,
        applicable_offering_ids=p.applicable_offering_ids,
        duration_kind=p.duration_kind,
        periods_total=p.periods_total,
        valid_from=p.valid_from,
        valid_to=p.valid_to,
        state=p.state,
    )


# ── TMF671 writes/reads (operator/admin) ───────────────────────────────────


@router.post(f"{_TMF}/promotion", response_model=Tmf671Promotion, status_code=201)
async def create_promotion(
    body: CreatePromotionRequest,
    svc: PromotionService = Depends(get_promotion_service),
) -> Tmf671Promotion:
    promo = await svc.create_promotion(
        promotion_id=body.promotion_id,
        discount_type=body.discount_type,
        discount_value=body.discount_value,
        duration_kind=body.duration_kind,
        audience=body.audience,
        currency=body.currency,
        code=body.code,
        promo_code_kind=body.promo_code_kind,
        applicable_offering_ids=body.applicable_offering_ids,
        periods_total=body.periods_total,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        display_name=body.display_name,
    )
    return _to_tmf671(promo)


@router.get(f"{_TMF}/promotion/{{promotion_id}}", response_model=Tmf671Promotion)
async def get_promotion(
    promotion_id: str,
    svc: PromotionService = Depends(get_promotion_service),
) -> Tmf671Promotion:
    promo = await svc.get(promotion_id)
    if promo is None:
        raise HTTPException(status_code=404, detail=f"Promotion {promotion_id} not found")
    return _to_tmf671(promo)


@router.get(f"{_TMF}/promotion", response_model=list[Tmf671Promotion])
async def list_promotions(
    state: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    svc: PromotionService = Depends(get_promotion_service),
) -> list[Tmf671Promotion]:
    promos = await svc.list_promotions(state=state, limit=limit, offset=offset)
    return [_to_tmf671(p) for p in promos]


@router.post(f"{_TMF}/promotion/{{promotion_id}}/assign")
async def assign_targeted(
    promotion_id: str,
    body: AssignTargetedRequest,
    svc: PromotionService = Depends(get_promotion_service),
) -> dict:
    return await svc.assign_targeted(
        promotion_id=promotion_id,
        customer_ids=body.customer_ids,
    )


# ── portal-facing reads ───────────────────────────────────────────────────


@router.get("/promo/preview")
async def preview_promo(
    code: str = Query(...),
    offering: str = Query(...),
    customer_id: str | None = Query(default=None, alias="customerId"),
    svc: PromotionService = Depends(get_promotion_service),
) -> dict:
    r = await svc.preview_promo(code=code, offering_id=offering, customer_id=customer_id)
    # money as strings for stable display rendering
    return {
        "valid": r["valid"],
        "code": r["code"],
        "offering": r["offering_id"],
        "label": r["label"],
        "name": r.get("name"),
        "base": str(r["base"]) if r["base"] is not None else None,
        "effective": str(r["effective"]) if r["effective"] is not None else None,
        "reason": r["reason"],
    }


@router.get("/promo/validate")
async def validate_promo(
    code: str = Query(...),
    offering: str = Query(...),
    customer_id: str | None = Query(default=None, alias="customerId"),
    svc: PromotionService = Depends(get_promotion_service),
) -> dict:
    """Full order-time validation — returns the discount *terms* COM stamps onto
    the order_item. ``customerId`` gates a targeted code on eligibility.
    """
    r = await svc.validate_for_order(code=code, offering_id=offering, customer_id=customer_id)
    return {
        "valid": r["valid"],
        "code": r["code"],
        "offering": r["offering_id"],
        "reason": r["reason"],
        "name": r.get("name"),
        "offerDefinitionId": r["offer_definition_id"],
        "discountType": r["discount_type"],
        "discountValue": str(r["discount_value"]) if r["discount_value"] is not None else None,
        "durationKind": r["duration_kind"],
        "periodsTotal": r["periods_total"],
        "discountPeriodsTotal": r["discount_periods_total"],
        "base": str(r["base"]) if r["base"] is not None else None,
        "effective": str(r["effective"]) if r["effective"] is not None else None,
        "label": r["label"],
    }


@router.get("/promo/resolve-eligible")
async def resolve_eligible(
    customer_id: str = Query(..., alias="customerId"),
    offering: str = Query(...),
    svc: PromotionService = Depends(get_promotion_service),
) -> dict:
    """Targeted-path order-time resolution (v1.1.1): the best targeted promo this
    customer is eligible for + applicable to the offering. Returns the promo's
    **code** (COM claims by code at activation, same path as a typed code)."""
    r = await svc.resolve_eligible_promo(customer_id=customer_id, offering_id=offering)
    if not r.get("valid"):
        return {"valid": False, "reason": r.get("reason")}
    return {
        "valid": True,
        "code": r["code"],
        "promotionId": r["promotion_id"],
        "name": r.get("name"),
        "offerDefinitionId": r["offer_definition_id"],
        "discountType": r["discount_type"],
        "discountValue": str(r["discount_value"]),
        "durationKind": r["duration_kind"],
        "periodsTotal": r["periods_total"],
        "discountPeriodsTotal": r["discount_periods_total"],
        "base": str(r["base"]),
        "effective": str(r["effective"]),
        "label": r["label"],
    }


@router.get("/promo/customer-offers")
async def customer_offers(
    customer_id: str = Query(..., alias="customerId"),
    state: str | None = Query(default=None),
    svc: PromotionService = Depends(get_promotion_service),
) -> dict:
    offers = await svc.list_customer_offers(customer_id=customer_id, state=state)
    return {"customerId": customer_id, "offers": offers}
