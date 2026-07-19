"""TMF621 Trouble Ticket endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_ticket_service
from app.schemas.tmf.ticket import (
    CreateTicketRequest,
    ResolveTicketRequest,
    Tmf621TroubleTicket,
    TransitionTicketRequest,
    UpdateTicketRequest,
    to_tmf621_ticket,
)
from app.services.ticket_service import TicketService

router = APIRouter(tags=["TMF621 Trouble Ticket"])


@router.post("/troubleTicket", response_model=Tmf621TroubleTicket, response_model_by_alias=True, status_code=201)
async def create_ticket(
    body: CreateTicketRequest,
    svc: TicketService = Depends(get_ticket_service),
) -> Tmf621TroubleTicket:
    ticket = await svc.open_ticket(
        customer_id=body.customer_id,
        subject=body.subject,
        description=body.description,
        ticket_type=body.ticket_type,
        priority=body.priority,
        case_id=body.case_id,
        assigned_to_agent_id=body.assigned_to_agent_id,
    )
    return to_tmf621_ticket(ticket)


@router.get("/troubleTicket", response_model=list[Tmf621TroubleTicket], response_model_by_alias=True)
async def list_tickets(
    customerId: str | None = None,
    caseId: str | None = None,
    state: str | None = None,
    assignedToAgentId: str | None = None,
    limit: int = 20,
    offset: int = 0,
    svc: TicketService = Depends(get_ticket_service),
) -> list[Tmf621TroubleTicket]:
    tickets = await svc.list_tickets(
        customer_id=customerId,
        case_id=caseId,
        state=state,
        assigned_to_agent_id=assignedToAgentId,
        limit=limit,
        offset=offset,
    )
    return [to_tmf621_ticket(t) for t in tickets]


@router.get("/troubleTicket/{ticket_id}", response_model=Tmf621TroubleTicket, response_model_by_alias=True)
async def get_ticket(
    ticket_id: str,
    svc: TicketService = Depends(get_ticket_service),
) -> Tmf621TroubleTicket:
    ticket = await svc.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return to_tmf621_ticket(ticket)


@router.patch("/troubleTicket/{ticket_id}", response_model=Tmf621TroubleTicket, response_model_by_alias=True)
async def update_ticket(
    ticket_id: str,
    body: UpdateTicketRequest,
    svc: TicketService = Depends(get_ticket_service),
) -> Tmf621TroubleTicket:
    updates = body.model_dump(exclude_none=True)
    ticket = await svc.update_ticket(ticket_id, **updates)
    return to_tmf621_ticket(ticket)


@router.post(
    "/troubleTicket/{ticket_id}/transition",
    response_model=Tmf621TroubleTicket,
    response_model_by_alias=True,
)
async def transition_ticket(
    ticket_id: str,
    body: TransitionTicketRequest,
    svc: TicketService = Depends(get_ticket_service),
) -> Tmf621TroubleTicket:
    ticket = await svc.transition_ticket(
        ticket_id,
        body.trigger,
        assigned_to_agent_id=body.assigned_to_agent_id,
        resolution_notes=body.resolution_notes,
        reason=body.reason,
    )
    return to_tmf621_ticket(ticket)


@router.post(
    "/troubleTicket/{ticket_id}/resolve",
    response_model=Tmf621TroubleTicket,
    response_model_by_alias=True,
)
async def resolve_ticket(
    ticket_id: str,
    body: ResolveTicketRequest,
    svc: TicketService = Depends(get_ticket_service),
) -> Tmf621TroubleTicket:
    ticket = await svc.resolve_ticket(ticket_id, resolution_notes=body.resolution_notes)
    return to_tmf621_ticket(ticket)


@router.post(
    "/troubleTicket/{ticket_id}/cancel",
    response_model=Tmf621TroubleTicket,
    response_model_by_alias=True,
)
async def cancel_ticket(
    ticket_id: str,
    svc: TicketService = Depends(get_ticket_service),
) -> Tmf621TroubleTicket:
    ticket = await svc.cancel_ticket(ticket_id)
    return to_tmf621_ticket(ticket)
