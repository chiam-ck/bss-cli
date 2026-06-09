"""TMF622 ProductOrder routes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_order_service
from app.schemas.order import (
    CreateOrderRequest,
    ProductOrderResponse,
    to_product_order_response,
)
from app.services.order_service import OrderService

router = APIRouter(tags=["productOrder"])


@router.post("/productOrder", response_model=ProductOrderResponse, status_code=201)
async def create_order(
    body: CreateOrderRequest,
    svc: OrderService = Depends(get_order_service),
):
    order = await svc.create_order(
        customer_id=body.customer_id,
        offering_id=body.offering_id,
        msisdn_preference=body.msisdn_preference,
        notes=body.notes,
        discount_code=body.discount_code,
        skip_assigned_offer=body.skip_assigned_offer,
    )
    return to_product_order_response(order)


@router.post("/productOrder/{order_id}/submit", response_model=ProductOrderResponse)
async def submit_order(
    order_id: str,
    svc: OrderService = Depends(get_order_service),
):
    order = await svc.submit_order(order_id)
    return to_product_order_response(order)


@router.post("/productOrder/{order_id}/cancel", response_model=ProductOrderResponse)
async def cancel_order(
    order_id: str,
    svc: OrderService = Depends(get_order_service),
):
    order = await svc.cancel_order(order_id)
    return to_product_order_response(order)


@router.get("/productOrder/{order_id}", response_model=ProductOrderResponse)
async def get_order(
    order_id: str,
    svc: OrderService = Depends(get_order_service),
):
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return to_product_order_response(order)


@router.get("/productOrder", response_model=list[ProductOrderResponse])
async def list_orders(
    customer_id: str | None = Query(default=None, alias="customerId"),
    state: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    svc: OrderService = Depends(get_order_service),
):
    orders = await svc.list_orders(
        customer_id=customer_id, state=state, limit=limit, offset=offset
    )
    return [to_product_order_response(o) for o in orders]
