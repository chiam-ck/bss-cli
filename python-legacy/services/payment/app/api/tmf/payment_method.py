"""TMF676 Payment Method Management router — no business logic."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_payment_method_service
from app.schemas.tmf.payment_method import (
    PaymentMethodCreateRequest,
    PaymentMethodResponse,
    to_payment_method_response,
)
from app.services.payment_method_service import PaymentMethodService

router = APIRouter(tags=["paymentMethod"])


@router.post(
    "/paymentMethod",
    response_model=PaymentMethodResponse,
    response_model_by_alias=True,
    status_code=201,
)
async def create_payment_method(
    body: PaymentMethodCreateRequest,
    svc: PaymentMethodService = Depends(get_payment_method_service),
) -> PaymentMethodResponse:
    pm = await svc.register_method(
        customer_id=body.customer_id,
        type_=body.type,
        tokenization_provider=body.tokenization_provider,
        provider_token=body.provider_token,
        brand=body.card_summary.brand,
        last4=body.card_summary.last4,
        exp_month=body.card_summary.exp_month,
        exp_year=body.card_summary.exp_year,
        country=body.card_summary.country,
    )
    return to_payment_method_response(pm, body.tokenization_provider)


@router.get(
    "/paymentMethod",
    response_model=list[PaymentMethodResponse],
    response_model_by_alias=True,
)
async def list_payment_methods(
    customerId: str,
    svc: PaymentMethodService = Depends(get_payment_method_service),
) -> list[PaymentMethodResponse]:
    methods = await svc.list_methods(customerId)
    return [to_payment_method_response(m) for m in methods]


@router.get(
    "/paymentMethod/{pm_id}",
    response_model=PaymentMethodResponse,
    response_model_by_alias=True,
)
async def get_payment_method(
    pm_id: str,
    svc: PaymentMethodService = Depends(get_payment_method_service),
) -> PaymentMethodResponse:
    pm = await svc.get_method(pm_id)
    if pm is None:
        raise HTTPException(status_code=404, detail=f"Payment method {pm_id} not found")
    return to_payment_method_response(pm)


@router.delete(
    "/paymentMethod/{pm_id}",
    response_model=PaymentMethodResponse,
    response_model_by_alias=True,
)
async def remove_payment_method(
    pm_id: str,
    svc: PaymentMethodService = Depends(get_payment_method_service),
) -> PaymentMethodResponse:
    pm = await svc.remove_method(pm_id)
    return to_payment_method_response(pm)


@router.post(
    "/paymentMethod/{pm_id}/setDefault",
    response_model=PaymentMethodResponse,
    response_model_by_alias=True,
)
async def set_default_payment_method(
    pm_id: str,
    svc: PaymentMethodService = Depends(get_payment_method_service),
) -> PaymentMethodResponse:
    """v0.10 — flip the default payment method for the owning customer."""
    pm = await svc.set_default_method(pm_id)
    return to_payment_method_response(pm)
