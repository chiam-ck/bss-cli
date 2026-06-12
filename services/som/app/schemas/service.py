"""TMF638 Service Pydantic schemas — camelCase for API."""

from datetime import datetime

from bss_models.service_inventory import Service
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

SVC_PATH = "/tmf-api/serviceInventoryManagement/v4/service"


class ServiceResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    href: str
    subscription_id: str | None = None
    spec_id: str
    type: str
    parent_service_id: str | None = None
    state: str
    characteristics: dict | None = None
    activated_at: datetime | None = None
    terminated_at: datetime | None = None
    children: list["ServiceResponse"] = []
    at_type: str = Field(default="Service", serialization_alias="@type")


def to_service_response(svc: Service) -> ServiceResponse:
    return ServiceResponse(
        id=svc.id,
        href=f"{SVC_PATH}/{svc.id}",
        subscription_id=svc.subscription_id,
        spec_id=svc.spec_id,
        type=svc.type,
        parent_service_id=svc.parent_service_id,
        state=svc.state,
        characteristics=svc.characteristics,
        activated_at=svc.activated_at,
        terminated_at=svc.terminated_at,
        children=[to_service_response(c) for c in svc.children] if svc.children else [],
    )
