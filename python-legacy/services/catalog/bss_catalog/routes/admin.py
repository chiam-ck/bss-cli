"""Catalog admin routes — offering / price write paths.

CLI-only surface for v0.7. The LLM tool registry does not include these;
operator catalog edits go through ``bss admin catalog ...`` only.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from bss_catalog.deps import get_session
from bss_catalog.repository import CatalogRepository
from bss_catalog.schemas.tmf620 import (
    Tmf620ProductOffering,
    Tmf620ProductOfferingPrice,
    to_tmf620_offering,
    to_tmf620_price,
)
from bss_catalog.services import CatalogAdminService

router = APIRouter(tags=["admin-catalog"])


class _CamelBase(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AddOfferingRequest(_CamelBase):
    offering_id: str
    name: str
    spec_id: str = "SPEC_MOBILE_PREPAID"
    amount: Decimal
    currency: str = "SGD"
    price_id: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    data_mb: int | None = None
    voice_minutes: int | None = None
    sms_count: int | None = None
    data_roaming_mb: int | None = None


class WindowOfferingRequest(_CamelBase):
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class AddPriceRequest(_CamelBase):
    price_id: str
    amount: Decimal
    currency: str = "SGD"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    retire_current: bool = False


@router.post("/admin/catalog/offering", response_model=Tmf620ProductOffering)
async def add_offering(
    body: AddOfferingRequest,
    session: AsyncSession = Depends(get_session),
    x_bss_actor: str = Header(default="anonymous"),
) -> Tmf620ProductOffering:
    repo = CatalogRepository(session)
    svc = CatalogAdminService(session, repo, actor=x_bss_actor)
    offering = await svc.add_offering(
        offering_id=body.offering_id,
        name=body.name,
        spec_id=body.spec_id,
        amount=body.amount,
        currency=body.currency,
        price_id=body.price_id,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        data_mb=body.data_mb,
        voice_minutes=body.voice_minutes,
        sms_count=body.sms_count,
        data_roaming_mb=body.data_roaming_mb,
    )
    return to_tmf620_offering(offering)


@router.patch(
    "/admin/catalog/offering/{offering_id}/window",
    response_model=Tmf620ProductOffering,
)
async def set_offering_window(
    offering_id: str,
    body: WindowOfferingRequest,
    session: AsyncSession = Depends(get_session),
    x_bss_actor: str = Header(default="anonymous"),
) -> Tmf620ProductOffering:
    repo = CatalogRepository(session)
    svc = CatalogAdminService(session, repo, actor=x_bss_actor)
    offering = await svc.set_offering_window(
        offering_id=offering_id,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
    )
    return to_tmf620_offering(offering)


@router.post(
    "/admin/catalog/offering/{offering_id}/retire",
    response_model=Tmf620ProductOffering,
)
async def retire_offering(
    offering_id: str,
    session: AsyncSession = Depends(get_session),
    x_bss_actor: str = Header(default="anonymous"),
) -> Tmf620ProductOffering:
    repo = CatalogRepository(session)
    svc = CatalogAdminService(session, repo, actor=x_bss_actor)
    offering = await svc.retire_offering(offering_id=offering_id)
    return to_tmf620_offering(offering)


@router.post(
    "/admin/catalog/offering/{offering_id}/price",
    response_model=Tmf620ProductOfferingPrice,
)
async def add_price(
    offering_id: str,
    body: AddPriceRequest,
    session: AsyncSession = Depends(get_session),
    x_bss_actor: str = Header(default="anonymous"),
) -> Tmf620ProductOfferingPrice:
    repo = CatalogRepository(session)
    svc = CatalogAdminService(session, repo, actor=x_bss_actor)
    price = await svc.add_price(
        offering_id=offering_id,
        price_id=body.price_id,
        amount=body.amount,
        currency=body.currency,
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        retire_current=body.retire_current,
    )
    return to_tmf620_price(price)
