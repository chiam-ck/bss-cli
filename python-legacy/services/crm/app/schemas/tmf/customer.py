"""TMF629 Customer Management schemas (camelCase)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class TmfBase(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ContactMediumSchema(TmfBase):
    id: str
    medium_type: str
    value: str
    is_primary: bool = False
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class IndividualSchema(TmfBase):
    given_name: str
    family_name: str
    date_of_birth: str | None = None


class Tmf629Customer(TmfBase):
    id: str
    href: str
    status: str
    kyc_status: str
    customer_since: datetime | None = None
    individual: IndividualSchema | None = None
    contact_medium: list[ContactMediumSchema] = []
    at_type: str = Field(default="Customer", serialization_alias="@type")


# ── Request schemas ─────────────────────────────────────────────────

class ContactMediumInput(TmfBase):
    medium_type: str
    value: str
    is_primary: bool = False


class CreateCustomerRequest(TmfBase):
    given_name: str
    family_name: str
    date_of_birth: str | None = None
    contact_medium: list[ContactMediumInput]


class UpdateCustomerRequest(TmfBase):
    status: str | None = None
    status_reason: str | None = None


class AddContactMediumRequest(TmfBase):
    medium_type: str
    value: str
    is_primary: bool = False


# ── Mapping ─────────────────────────────────────────────────────────

CUSTOMER_PATH = "/tmf-api/customerManagement/v4/customer"


def to_tmf629_customer(cust, party=None) -> Tmf629Customer:
    individual = None
    contact_mediums = []

    p = getattr(cust, "_party", None) or party
    if p:
        if p.individual:
            individual = IndividualSchema(
                given_name=p.individual.given_name,
                family_name=p.individual.family_name,
                date_of_birth=str(p.individual.date_of_birth) if p.individual.date_of_birth else None,
            )
        contact_mediums = [
            ContactMediumSchema(
                id=cm.id,
                medium_type=cm.medium_type,
                value=cm.value,
                is_primary=cm.is_primary,
                valid_from=cm.valid_from,
                valid_to=cm.valid_to,
            )
            for cm in (p.contact_mediums or [])
            if cm.valid_to is None
        ]

    return Tmf629Customer(
        id=cust.id,
        href=f"{CUSTOMER_PATH}/{cust.id}",
        status=cust.status,
        kyc_status=cust.kyc_status,
        customer_since=cust.customer_since,
        individual=individual,
        contact_medium=contact_mediums,
    )
