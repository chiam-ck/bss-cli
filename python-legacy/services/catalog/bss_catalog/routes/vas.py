from fastapi import APIRouter, Depends, HTTPException

from bss_catalog.deps import get_repo
from bss_catalog.repository import CatalogRepository
from bss_catalog.schemas.vas import VasOfferingSchema, to_vas_offering

router = APIRouter(tags=["vas"])


@router.get("/offering", response_model=list[VasOfferingSchema], response_model_by_alias=True)
async def list_vas_offerings(
    limit: int = 20,
    offset: int = 0,
    repo: CatalogRepository = Depends(get_repo),
) -> list[VasOfferingSchema]:
    models = await repo.list_vas_offerings(limit=limit, offset=offset)
    return [to_vas_offering(m) for m in models]


@router.get("/offering/{vas_id}", response_model=VasOfferingSchema, response_model_by_alias=True)
async def get_vas_offering(
    vas_id: str,
    repo: CatalogRepository = Depends(get_repo),
) -> VasOfferingSchema:
    model = await repo.get_vas_offering(vas_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"VAS offering {vas_id} not found")
    return to_vas_offering(model)
