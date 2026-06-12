"""VAS offering response schemas — custom (no TMF spec for VAS)."""

from bss_models.catalog import VasOffering as VasOfferingModel
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class VasBase(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class VasOfferingSchema(VasBase):
    id: str
    name: str | None = None
    price_amount: float
    currency: str
    allowance_type: str | None = None
    allowance_quantity: int | None = None
    allowance_unit: str | None = None
    expiry_hours: int | None = None


def to_vas_offering(model: VasOfferingModel) -> VasOfferingSchema:
    return VasOfferingSchema(
        id=model.id,
        name=model.name,
        price_amount=float(model.price_amount),
        currency=model.currency,
        allowance_type=model.allowance_type,
        allowance_quantity=model.allowance_quantity,
        allowance_unit=model.allowance_unit,
        expiry_hours=model.expiry_hours,
    )
