"""eSIM profile endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_inventory_service
from app.schemas.internal.inventory import (
    AssignMsisdnRequest,
    EsimActivationResponse,
    EsimResponse,
    to_esim_response,
)
from app.services.inventory_service import InventoryService

router = APIRouter(tags=["Inventory eSIM"])


@router.get("/esim", response_model=list[EsimResponse])
async def list_esims(
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
    svc: InventoryService = Depends(get_inventory_service),
) -> list[EsimResponse]:
    rows = await svc.list_esims(status=status, limit=limit, offset=offset)
    return [to_esim_response(r) for r in rows]


@router.get("/esim/{iccid}", response_model=EsimResponse)
async def get_esim(
    iccid: str,
    svc: InventoryService = Depends(get_inventory_service),
) -> EsimResponse:
    row = await svc.get_esim(iccid)
    if not row:
        raise HTTPException(status_code=404, detail=f"eSIM {iccid} not found")
    return to_esim_response(row)


@router.post("/esim/reserve", response_model=EsimResponse, status_code=201)
async def reserve_esim(
    svc: InventoryService = Depends(get_inventory_service),
) -> EsimResponse:
    row = await svc.reserve_esim()
    return to_esim_response(row)


@router.post("/esim/{iccid}/assign-msisdn", response_model=EsimResponse)
async def assign_msisdn(
    iccid: str,
    body: AssignMsisdnRequest,
    svc: InventoryService = Depends(get_inventory_service),
) -> EsimResponse:
    row = await svc.assign_msisdn_to_esim(iccid, body.msisdn)
    return to_esim_response(row)


@router.post("/esim/{iccid}/mark-downloaded", response_model=EsimResponse)
async def mark_downloaded(
    iccid: str,
    svc: InventoryService = Depends(get_inventory_service),
) -> EsimResponse:
    row = await svc.mark_downloaded(iccid)
    return to_esim_response(row)


@router.post("/esim/{iccid}/mark-activated", response_model=EsimResponse)
async def mark_activated(
    iccid: str,
    svc: InventoryService = Depends(get_inventory_service),
) -> EsimResponse:
    row = await svc.mark_activated(iccid)
    return to_esim_response(row)


@router.post("/esim/{iccid}/recycle", response_model=EsimResponse)
async def recycle_esim(
    iccid: str,
    svc: InventoryService = Depends(get_inventory_service),
) -> EsimResponse:
    row = await svc.recycle_esim(iccid)
    return to_esim_response(row)


@router.post("/esim/{iccid}/release", response_model=EsimResponse)
async def release_esim(
    iccid: str,
    svc: InventoryService = Depends(get_inventory_service),
) -> EsimResponse:
    row = await svc.release_esim(iccid)
    return to_esim_response(row)


@router.get("/esim/{iccid}/activation", response_model=EsimActivationResponse)
async def get_activation_code(
    iccid: str,
    svc: InventoryService = Depends(get_inventory_service),
) -> EsimActivationResponse:
    data = await svc.get_activation_code(iccid)
    return EsimActivationResponse(**data)
