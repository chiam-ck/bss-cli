"""TMF620 Product Catalog Management v4 — Pydantic response models.

Field names are snake_case in Python; alias_generator produces camelCase JSON
output matching the TMF620 specification exactly.
"""

from datetime import datetime

from bss_models.catalog import (
    ProductOffering as ProductOfferingModel,
)
from bss_models.catalog import (
    ProductOfferingPrice as ProductOfferingPriceModel,
)
from bss_models.catalog import (
    ProductSpecification as ProductSpecificationModel,
)
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class TmfBase(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class TimePeriod(TmfBase):
    start_date_time: datetime | None = None
    end_date_time: datetime | None = None


class Money(BaseModel):
    value: float
    unit: str


class ProductPrice(TmfBase):
    tax_included_amount: Money


class Tmf620ProductOfferingPrice(TmfBase):
    id: str
    price_type: str
    recurring_charge_period_length: int | None = None
    recurring_charge_period_type: str | None = None
    price: ProductPrice
    valid_for: TimePeriod | None = None
    at_type: str = Field(default="ProductOfferingPrice", serialization_alias="@type")


class ProductSpecificationRef(TmfBase):
    id: str
    href: str
    name: str | None = None
    at_type: str = Field(default="ProductSpecificationRef", serialization_alias="@type")


class BundleAllowanceItem(TmfBase):
    """Custom extension — TMF620 does not define bundle allowances directly."""

    allowance_type: str
    quantity: int
    unit: str


class Tmf620ProductOffering(TmfBase):
    id: str
    href: str
    name: str | None = None
    is_bundle: bool
    is_sellable: bool | None = None
    lifecycle_status: str | None = None
    valid_for: TimePeriod | None = None
    product_specification: ProductSpecificationRef | None = None
    product_offering_price: list[Tmf620ProductOfferingPrice] = []
    bundle_allowance: list[BundleAllowanceItem] = []
    at_type: str = Field(default="ProductOffering", serialization_alias="@type")


class Tmf620ProductSpecification(TmfBase):
    id: str
    href: str
    name: str | None = None
    description: str | None = None
    brand: str | None = None
    lifecycle_status: str | None = None
    at_type: str = Field(default="ProductSpecification", serialization_alias="@type")


# --- Mapping functions: ORM model → TMF schema ---

TMF_OFFERING_PATH = "/tmf-api/productCatalogManagement/v4/productOffering"
TMF_OFFERING_PRICE_PATH = "/tmf-api/productCatalogManagement/v4/productOfferingPrice"
TMF_SPEC_PATH = "/tmf-api/productCatalogManagement/v4/productSpecification"


def to_tmf620_price(model: ProductOfferingPriceModel) -> Tmf620ProductOfferingPrice:
    valid_for = None
    if model.valid_from or model.valid_to:
        valid_for = TimePeriod(
            start_date_time=model.valid_from,
            end_date_time=model.valid_to,
        )
    return Tmf620ProductOfferingPrice(
        id=model.id,
        price_type=model.price_type,
        recurring_charge_period_length=model.recurring_period_length,
        recurring_charge_period_type=model.recurring_period_type,
        price=ProductPrice(
            tax_included_amount=Money(value=float(model.amount), unit=model.currency),
        ),
        valid_for=valid_for,
    )


def to_tmf620_offering(model: ProductOfferingModel) -> Tmf620ProductOffering:
    spec_ref = None
    if model.specification:
        spec_ref = ProductSpecificationRef(
            id=model.specification.id,
            href=f"{TMF_SPEC_PATH}/{model.specification.id}",
            name=model.specification.name,
        )

    prices = [to_tmf620_price(p) for p in model.prices]

    allowances = [
        BundleAllowanceItem(
            allowance_type=a.allowance_type,
            quantity=a.quantity,
            unit=a.unit,
        )
        for a in model.allowances
    ]

    valid_for = None
    if model.valid_from or model.valid_to:
        valid_for = TimePeriod(
            start_date_time=model.valid_from,
            end_date_time=model.valid_to,
        )

    return Tmf620ProductOffering(
        id=model.id,
        href=f"{TMF_OFFERING_PATH}/{model.id}",
        name=model.name,
        is_bundle=model.is_bundle,
        is_sellable=model.is_sellable,
        lifecycle_status=model.lifecycle_status,
        valid_for=valid_for,
        product_specification=spec_ref,
        product_offering_price=prices,
        bundle_allowance=allowances,
    )


def to_tmf620_specification(model: ProductSpecificationModel) -> Tmf620ProductSpecification:
    return Tmf620ProductSpecification(
        id=model.id,
        href=f"{TMF_SPEC_PATH}/{model.id}",
        name=model.name,
        description=model.description,
        brand=model.brand,
        lifecycle_status=model.lifecycle_status,
    )
