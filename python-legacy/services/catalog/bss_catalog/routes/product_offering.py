from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from bss_catalog.deps import get_repo
from bss_catalog.repository import CatalogRepository
from bss_catalog.schemas.tmf620 import Tmf620ProductOffering, to_tmf620_offering

router = APIRouter(tags=["productOffering"])


@router.get("/productOffering", response_model=list[Tmf620ProductOffering], response_model_by_alias=True)
async def list_offerings(
    lifecycleStatus: str | None = None,
    activeAt: datetime | None = Query(
        default=None,
        description=(
            "ISO-8601 moment to filter time-bound rows. When supplied, only "
            "offerings sellable at that instant are returned and they are "
            "ordered by lowest active recurring price."
        ),
    ),
    limit: int = 20,
    offset: int = 0,
    repo: CatalogRepository = Depends(get_repo),
) -> list[Tmf620ProductOffering]:
    if activeAt is not None:
        models = await repo.list_active_offerings(at=activeAt, limit=limit, offset=offset)
    else:
        models = await repo.list_offerings(
            lifecycle_status=lifecycleStatus,
            limit=limit,
            offset=offset,
        )
    return [to_tmf620_offering(m) for m in models]


@router.get("/productOffering/{offering_id}", response_model=Tmf620ProductOffering, response_model_by_alias=True)
async def get_offering(
    offering_id: str,
    repo: CatalogRepository = Depends(get_repo),
) -> Tmf620ProductOffering:
    model = await repo.get_offering(offering_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"ProductOffering {offering_id} not found")
    return to_tmf620_offering(model)
