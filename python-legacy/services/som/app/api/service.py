"""TMF638 Service API routes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_som_service
from app.schemas.service import ServiceResponse, to_service_response
from app.services.som_service import SOMService

router = APIRouter(tags=["service"])


@router.get("/service/{service_id}", response_model=ServiceResponse)
async def get_service(
    service_id: str,
    svc: SOMService = Depends(get_som_service),
):
    service = await svc.get_service(service_id)
    if not service:
        raise HTTPException(status_code=404, detail=f"Service {service_id} not found")
    return to_service_response(service)


@router.get("/service", response_model=list[ServiceResponse])
async def list_services(
    subscription_id: str = Query(alias="subscriptionId"),
    svc: SOMService = Depends(get_som_service),
):
    services = await svc.list_services_for_subscription(subscription_id)
    return [to_service_response(s) for s in services]
