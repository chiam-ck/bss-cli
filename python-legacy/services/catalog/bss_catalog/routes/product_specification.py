from fastapi import APIRouter, Depends, HTTPException

from bss_catalog.deps import get_repo
from bss_catalog.repository import CatalogRepository
from bss_catalog.schemas.tmf620 import Tmf620ProductSpecification, to_tmf620_specification

router = APIRouter(tags=["productSpecification"])


@router.get(
    "/productSpecification",
    response_model=list[Tmf620ProductSpecification],
    response_model_by_alias=True,
)
async def list_specifications(
    limit: int = 20,
    offset: int = 0,
    repo: CatalogRepository = Depends(get_repo),
) -> list[Tmf620ProductSpecification]:
    models = await repo.list_specifications(limit=limit, offset=offset)
    return [to_tmf620_specification(m) for m in models]


@router.get(
    "/productSpecification/{spec_id}",
    response_model=Tmf620ProductSpecification,
    response_model_by_alias=True,
)
async def get_specification(
    spec_id: str,
    repo: CatalogRepository = Depends(get_repo),
) -> Tmf620ProductSpecification:
    model = await repo.get_specification(spec_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"ProductSpecification {spec_id} not found")
    return to_tmf620_specification(model)
