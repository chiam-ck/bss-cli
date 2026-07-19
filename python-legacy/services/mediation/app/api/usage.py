"""TMF635 Usage Management API — online mediation ingress."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_mediation_service
from app.schemas.usage import UsageCreateRequest, UsageResponse, to_usage_response
from app.services.mediation_service import MediationService

router = APIRouter(tags=["Usage Management"])


@router.post("/usage", response_model=UsageResponse, status_code=201)
async def create_usage(
    body: UsageCreateRequest,
    svc: MediationService = Depends(get_mediation_service),
) -> UsageResponse:
    evt = await svc.ingest(
        msisdn=body.msisdn,
        event_type=body.event_type,
        event_time=body.event_time,
        quantity=body.quantity,
        unit=body.unit,
        source=body.source,
        raw_cdr_ref=body.raw_cdr_ref,
        roaming_indicator=body.roaming_indicator,
    )
    return to_usage_response(evt)


@router.get("/usage/{event_id}", response_model=UsageResponse)
async def get_usage(
    event_id: str,
    svc: MediationService = Depends(get_mediation_service),
) -> UsageResponse:
    evt = await svc.get(event_id)
    if not evt:
        raise HTTPException(status_code=404, detail=f"Usage event {event_id} not found")
    return to_usage_response(evt)


@router.get("/usage", response_model=list[UsageResponse])
async def list_usage(
    subscriptionId: str | None = Query(default=None),
    msisdn: str | None = Query(default=None),
    type: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    svc: MediationService = Depends(get_mediation_service),
) -> list[UsageResponse]:
    events = await svc.list_by_filters(
        subscription_id=subscriptionId,
        msisdn=msisdn,
        event_type=type,
        since=since,
        limit=limit,
    )
    return [to_usage_response(e) for e in events]
