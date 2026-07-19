"""v0.17 — PortRequest DTOs (camelCase on the wire)."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class PortRequestResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    direction: str
    donor_carrier: str
    donor_msisdn: str
    target_subscription_id: str | None
    requested_port_date: date
    state: str
    rejection_reason: str | None
    created_at: datetime
    updated_at: datetime


class CreatePortRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    direction: str
    donor_carrier: str
    donor_msisdn: str
    target_subscription_id: str | None = None
    requested_port_date: date


class RejectPortRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    reason: str


def to_port_request_response(p) -> PortRequestResponse:
    return PortRequestResponse(
        id=p.id,
        direction=p.direction,
        donor_carrier=p.donor_carrier,
        donor_msisdn=p.donor_msisdn,
        target_subscription_id=p.target_subscription_id,
        requested_port_date=p.requested_port_date,
        state=p.state,
        rejection_reason=p.rejection_reason,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )
