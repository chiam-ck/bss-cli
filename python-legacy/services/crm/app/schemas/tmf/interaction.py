"""TMF683 Customer Interaction schemas (camelCase)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class TmfBase(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class Tmf683Interaction(TmfBase):
    id: str
    customer_id: str
    channel: str | None = None
    direction: str | None = None
    summary: str
    body: str | None = None
    agent_id: str | None = None
    related_case_id: str | None = None
    related_ticket_id: str | None = None
    occurred_at: datetime
    at_type: str = Field(default="Interaction", serialization_alias="@type")


class CreateInteractionRequest(TmfBase):
    customer_id: str
    channel: str | None = None
    direction: str = "inbound"
    summary: str
    body: str | None = None
    agent_id: str | None = None
    related_case_id: str | None = None
    related_ticket_id: str | None = None


def to_tmf683_interaction(i) -> Tmf683Interaction:
    return Tmf683Interaction(
        id=i.id,
        customer_id=i.customer_id,
        channel=i.channel,
        direction=i.direction,
        summary=i.summary,
        body=i.body,
        agent_id=i.agent_id,
        related_case_id=i.related_case_id,
        related_ticket_id=i.related_ticket_id,
        occurred_at=i.occurred_at,
    )
