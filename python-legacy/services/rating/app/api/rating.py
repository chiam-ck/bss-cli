"""Rating debug/inspection endpoints.

GET /tariff/{offering_id}  → passthrough to Catalog for inspection
POST /rate-test            → apply `rate_usage` to a given event, no persist
"""

from decimal import Decimal

from bss_clients import CatalogClient
from bss_clients.errors import NotFound
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.dependencies import get_catalog_client
from app.domain.rating import UsageInput, rate_usage

router = APIRouter(tags=["Rating"])


class RateTestRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    usage_event_id: str = "UE-TEST"
    subscription_id: str
    msisdn: str
    offering_id: str
    event_type: str
    quantity: int
    unit: str


class RateTestResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    usage_event_id: str
    subscription_id: str
    allowance_type: str
    consumed_quantity: int
    unit: str
    charge_amount: str
    currency: str


@router.get("/tariff/{offering_id}")
async def get_tariff(
    offering_id: str,
    catalog: CatalogClient = Depends(get_catalog_client),
) -> dict:
    try:
        return await catalog.get_offering(offering_id)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"Offering {offering_id} not found")


@router.post("/rate-test", response_model=RateTestResponse)
async def rate_test(
    body: RateTestRequest,
    catalog: CatalogClient = Depends(get_catalog_client),
) -> RateTestResponse:
    try:
        tariff = await catalog.get_offering(body.offering_id)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"Offering {body.offering_id} not found")

    usage = UsageInput(
        usage_event_id=body.usage_event_id,
        subscription_id=body.subscription_id,
        msisdn=body.msisdn,
        event_type=body.event_type,
        quantity=body.quantity,
        unit=body.unit,
    )
    result = rate_usage(usage, tariff)

    # charge_amount is a Decimal; surface as string for JSON stability
    charge_str = str(
        result.charge_amount
        if isinstance(result.charge_amount, Decimal)
        else Decimal(result.charge_amount)
    )

    return RateTestResponse(
        usage_event_id=result.usage_event_id,
        subscription_id=result.subscription_id,
        allowance_type=result.allowance_type,
        consumed_quantity=result.consumed_quantity,
        unit=result.unit,
        charge_amount=charge_str,
        currency=result.currency,
    )
