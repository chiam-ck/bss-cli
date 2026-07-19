"""Fault injection API routers — no business logic."""

from fastapi import APIRouter, Depends

from app.dependencies import get_provisioning_service
from app.schemas.task import (
    FaultInjectionResponse,
    FaultInjectionUpdateRequest,
    to_fault_injection_response,
)
from app.services.provisioning_service import ProvisioningService

router = APIRouter(tags=["Fault Injection"])


@router.get("/fault-injection", response_model=list[FaultInjectionResponse])
async def list_fault_rules(
    svc: ProvisioningService = Depends(get_provisioning_service),
) -> list[FaultInjectionResponse]:
    rules = await svc.list_fault_rules()
    return [to_fault_injection_response(r) for r in rules]


@router.patch("/fault-injection/{fault_id}", response_model=FaultInjectionResponse)
async def update_fault_rule(
    fault_id: str,
    body: FaultInjectionUpdateRequest,
    svc: ProvisioningService = Depends(get_provisioning_service),
) -> FaultInjectionResponse:
    fault = await svc.update_fault_rule(
        fault_id,
        enabled=body.enabled,
        probability=body.probability,
        fault_type=body.fault_type,
    )
    return to_fault_injection_response(fault)
