"""TMF621 Trouble Ticket schemas (camelCase)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class TmfBase(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class Tmf621TroubleTicket(TmfBase):
    id: str
    href: str
    ticket_type: str | None = None
    subject: str
    description: str | None = None
    state: str
    priority: str | None = None
    customer_id: str
    case_id: str | None = None
    assigned_to_agent_id: str | None = None
    resolution_notes: str | None = None
    opened_at: datetime
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    at_type: str = Field(default="TroubleTicket", serialization_alias="@type")


class CreateTicketRequest(TmfBase):
    customer_id: str
    subject: str
    description: str | None = None
    ticket_type: str = "information_request"
    priority: str = "normal"
    case_id: str | None = None
    assigned_to_agent_id: str | None = None


class UpdateTicketRequest(TmfBase):
    priority: str | None = None
    assigned_to_agent_id: str | None = None
    description: str | None = None


class ResolveTicketRequest(TmfBase):
    resolution_notes: str


class TransitionTicketRequest(TmfBase):
    trigger: str
    assigned_to_agent_id: str | None = None
    resolution_notes: str | None = None
    reason: str | None = None


TICKET_PATH = "/tmf-api/troubleTicket/v4/troubleTicket"


def to_tmf621_ticket(t) -> Tmf621TroubleTicket:
    return Tmf621TroubleTicket(
        id=t.id,
        href=f"{TICKET_PATH}/{t.id}",
        ticket_type=t.ticket_type,
        subject=t.subject,
        description=t.description,
        state=t.state,
        priority=t.priority,
        customer_id=t.customer_id,
        case_id=t.case_id,
        assigned_to_agent_id=t.assigned_to_agent_id,
        resolution_notes=t.resolution_notes,
        opened_at=t.opened_at,
        resolved_at=t.resolved_at,
        closed_at=t.closed_at,
    )
