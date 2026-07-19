"""TMF641 ServiceOrder API routes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_som_service
from app.schemas.service_order import ServiceOrderResponse, to_service_order_response
from app.services.som_service import SOMService

router = APIRouter(tags=["serviceOrder"])


@router.get("/serviceOrder/{order_id}", response_model=ServiceOrderResponse)
async def get_service_order(
    order_id: str,
    svc: SOMService = Depends(get_som_service),
):
    so = await svc.get_service_order(order_id)
    if not so:
        raise HTTPException(status_code=404, detail=f"ServiceOrder {order_id} not found")
    return to_service_order_response(so)


@router.get("/serviceOrder", response_model=list[ServiceOrderResponse])
async def list_service_orders(
    commercial_order_id: str = Query(alias="commercialOrderId"),
    svc: SOMService = Depends(get_som_service),
):
    orders = await svc.list_service_orders_for_commercial(commercial_order_id)
    return [to_service_order_response(so) for so in orders]
