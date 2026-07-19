"""v0.17 — PortRequest endpoints (operator-driven MNP).

Mounted under ``/crm-api/v1`` alongside the Case routes. No customer
self-serve path (doctrine v0.17+); the cockpit + REPL are the only
write callers.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_port_request_service
from app.schemas.internal.port_request import (
    CreatePortRequest,
    PortRequestResponse,
    RejectPortRequest,
    to_port_request_response,
)
from app.services.port_request_service import PortRequestService

router = APIRouter(tags=["Port Request"])


@router.get("/port-requests", response_model=list[PortRequestResponse])
async def list_port_requests(
    state: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    offset: int = 0,
    svc: PortRequestService = Depends(get_port_request_service),
) -> list[PortRequestResponse]:
    rows = await svc.list_requests(
        state=state, direction=direction, limit=limit, offset=offset
    )
    return [to_port_request_response(r) for r in rows]


@router.get("/port-requests/{port_id}", response_model=PortRequestResponse)
async def get_port_request(
    port_id: str,
    svc: PortRequestService = Depends(get_port_request_service),
) -> PortRequestResponse:
    row = await svc.get(port_id)
    if not row:
        raise HTTPException(
            status_code=404, detail=f"Port request {port_id} not found"
        )
    return to_port_request_response(row)


@router.post(
    "/port-requests",
    response_model=PortRequestResponse,
    status_code=201,
)
async def create_port_request(
    body: CreatePortRequest,
    svc: PortRequestService = Depends(get_port_request_service),
) -> PortRequestResponse:
    port = await svc.create(
        direction=body.direction,
        donor_carrier=body.donor_carrier,
        donor_msisdn=body.donor_msisdn,
        target_subscription_id=body.target_subscription_id,
        requested_port_date=body.requested_port_date,
    )
    # Re-fetch so the response carries server_default-populated
    # created_at / updated_at without relying on a post-commit lazy
    # load (mirrors the case route pattern).
    port = await svc.get(port.id)
    return to_port_request_response(port)


@router.post(
    "/port-requests/{port_id}/approve",
    response_model=PortRequestResponse,
)
async def approve_port_request(
    port_id: str,
    svc: PortRequestService = Depends(get_port_request_service),
) -> PortRequestResponse:
    await svc.approve(port_id)
    port = await svc.get(port_id)
    return to_port_request_response(port)


@router.post(
    "/port-requests/{port_id}/reject",
    response_model=PortRequestResponse,
)
async def reject_port_request(
    port_id: str,
    body: RejectPortRequest,
    svc: PortRequestService = Depends(get_port_request_service),
) -> PortRequestResponse:
    await svc.reject(port_id, body.reason)
    port = await svc.get(port_id)
    return to_port_request_response(port)
