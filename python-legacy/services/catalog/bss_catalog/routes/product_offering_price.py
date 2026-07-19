"""Product offering price routes — v0.7 active-aware lookups.

The base TMF resource (`/productOfferingPrice`) is intentionally not
exposed for listing here; promo prices are not customer-facing inventory.
The active lookup is the only public surface — renewal reads the
snapshot off the subscription, never this endpoint.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from bss_catalog.deps import get_repo
from bss_catalog.repository import CatalogRepository
from bss_catalog.schemas.tmf620 import Tmf620ProductOfferingPrice, to_tmf620_price

router = APIRouter(tags=["productOfferingPrice"])


@router.get(
    "/productOfferingPrice/{price_id}",
    response_model=Tmf620ProductOfferingPrice,
    response_model_by_alias=True,
)
async def get_offering_price(
    price_id: str,
    repo: CatalogRepository = Depends(get_repo),
) -> Tmf620ProductOfferingPrice:
    """Direct lookup by price id — used by renewal-time math (snapshot resolve)."""
    model = await repo.get_offering_price_by_id(price_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"ProductOfferingPrice {price_id} not found")
    return to_tmf620_price(model)


@router.get(
    "/productOfferingPrice/active/{offering_id}",
    response_model=Tmf620ProductOfferingPrice,
    response_model_by_alias=True,
)
async def get_active_price_for_offering(
    offering_id: str,
    activeAt: datetime | None = Query(
        default=None,
        description="ISO-8601 moment to evaluate (defaults to now).",
    ),
    repo: CatalogRepository = Depends(get_repo),
) -> Tmf620ProductOfferingPrice:
    """Lowest-amount active price row for `offering_id` at `activeAt`.

    Raises 422 POLICY_VIOLATION (`catalog.price.no_active_row`) when no
    row matches — renewal must never silently fall back.
    """
    model = await repo.get_active_price(offering_id, at=activeAt)
    return to_tmf620_price(model)
