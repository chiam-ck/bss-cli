"""TMF641 ServiceOrder Pydantic schemas — camelCase for API."""

from datetime import datetime

from bss_models.service_inventory import ServiceOrder, ServiceOrderItem
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

SO_PATH = "/tmf-api/serviceOrderingManagement/v4/serviceOrder"


class ServiceOrderItemResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    action: str
    service_spec_id: str
    target_service_id: str | None = None


class ServiceOrderResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    href: str
    commercial_order_id: str
    state: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    items: list[ServiceOrderItemResponse] = []
    at_type: str = Field(default="ServiceOrder", serialization_alias="@type")


def to_item_response(item: ServiceOrderItem) -> ServiceOrderItemResponse:
    return ServiceOrderItemResponse(
        id=item.id,
        action=item.action,
        service_spec_id=item.service_spec_id,
        target_service_id=item.target_service_id,
    )


def to_service_order_response(so: ServiceOrder) -> ServiceOrderResponse:
    return ServiceOrderResponse(
        id=so.id,
        href=f"{SO_PATH}/{so.id}",
        commercial_order_id=so.commercial_order_id,
        state=so.state,
        started_at=so.started_at,
        completed_at=so.completed_at,
        items=[to_item_response(i) for i in so.items] if so.items else [],
    )
