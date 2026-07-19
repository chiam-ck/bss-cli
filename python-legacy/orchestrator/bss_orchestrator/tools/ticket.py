"""Trouble-ticket tools — TMF621 Trouble Ticket surface."""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import (
    AgentId,
    CaseId,
    CustomerId,
    OrderId,
    ServiceId,
    SubscriptionId,
    TicketId,
    TicketState,
    TicketType,
)
from ._registry import register


@register("ticket.open")
async def ticket_open(
    ticket_type: TicketType,
    subject: str,
    case_id: CaseId | None = None,
    customer_id: CustomerId | None = None,
    order_id: OrderId | None = None,
    subscription_id: SubscriptionId | None = None,
    service_id: ServiceId | None = None,
) -> dict[str, Any]:
    """Open a trouble ticket. Attach it to exactly ONE primary entity via the
    most specific available ID: service > subscription > order > case > customer.

    Args:
        ticket_type: One of ``service_outage``, ``billing_issue``,
            ``information``, ``complaint``, ``configuration_change``,
            ``fraud_report``, ``cancellation_request``.
        subject: Short description of the issue.
        case_id: Parent case (CASE-... opaque suffix) if this ticket belongs to one.
        customer_id: Linked customer (CUST-... opaque suffix).
        order_id: Linked order (ORD-... opaque suffix).
        subscription_id: Linked subscription (SUB-... opaque suffix).
        service_id: Linked service (SVC-... opaque suffix).

    Returns:
        The created ticket ``{id, ticketType, state, priority, relatedEntity}``.

    Raises:
        PolicyViolationFromServer:
            - ``ticket.open.requires_at_least_one_link``: at least one of the
              linking IDs must be provided.
    """
    return await get_clients().crm.open_ticket(
        ticket_type=ticket_type,
        subject=subject,
        case_id=case_id,
        customer_id=customer_id,
        order_id=order_id,
        subscription_id=subscription_id,
        service_id=service_id,
    )


@register("ticket.get")
async def ticket_get(ticket_id: TicketId) -> dict[str, Any]:
    """Read a trouble ticket with full state history.

    Args:
        ticket_id: Ticket ID with the TKT- prefix (opaque suffix).

    Returns:
        Ticket dict including ``stateHistory`` and ``assignedAgent``.

    Raises:
        NotFound: no ticket with this ID.
    """
    return await get_clients().crm.get_ticket(ticket_id)


@register("ticket.list")
async def ticket_list(
    customer_id: CustomerId | None = None,
    case_id: CaseId | None = None,
    state: TicketState | None = None,
    agent_id: AgentId | None = None,
) -> list[dict[str, Any]]:
    """List tickets, optionally filtered.

    Args:
        customer_id: Filter by customer.
        case_id: Filter by parent case.
        state: Filter by ticket state.
        agent_id: Filter by assigned agent.

    Returns:
        List of ticket dicts (may be empty).

    Raises:
        (none expected — read tool)
    """
    return await get_clients().crm.list_tickets(
        customer_id=customer_id, case_id=case_id, state=state, agent_id=agent_id
    )


@register("ticket.assign")
async def ticket_assign(ticket_id: TicketId, agent_id: AgentId) -> dict[str, Any]:
    """Assign a ticket to an agent. Agent must be in ``active`` state.

    Args:
        ticket_id: Ticket ID with the TKT- prefix (opaque suffix).
        agent_id: Agent ID with the AGT- prefix (opaque suffix). Get from ``agents.list``.

    Returns:
        Updated ticket dict with ``assignedAgent`` set.

    Raises:
        PolicyViolationFromServer:
            - ``ticket.assign.agent_must_be_active``: choose an active agent.
    """
    return await get_clients().crm.assign_ticket(ticket_id, agent_id=agent_id)


@register("ticket.transition")
async def ticket_transition(
    ticket_id: TicketId, to_state: TicketState
) -> dict[str, Any]:
    """Explicitly transition a ticket to another state. Prefer
    ``ticket.resolve`` and ``ticket.close`` for those specific transitions.

    Args:
        ticket_id: Ticket ID with the TKT- prefix (opaque suffix).
        to_state: Target state.

    Returns:
        Updated ticket dict.

    Raises:
        PolicyViolationFromServer:
            - ``ticket.transition.illegal``: state machine forbids this move.
    """
    return await get_clients().crm.transition_ticket(ticket_id, to_state=to_state)


@register("ticket.resolve")
async def ticket_resolve(
    ticket_id: TicketId, resolution_notes: str
) -> dict[str, Any]:
    """Resolve a ticket with mandatory resolution notes.

    Args:
        ticket_id: Ticket ID with the TKT- prefix (opaque suffix).
        resolution_notes: Free text — what fixed the issue.

    Returns:
        Updated ticket dict with ``state="resolved"``.

    Raises:
        PolicyViolationFromServer:
            - ``ticket.resolve.requires_resolution_notes``: pass a non-empty
              ``resolution_notes``.
    """
    return await get_clients().crm.resolve_ticket(
        ticket_id, resolution_notes=resolution_notes
    )


@register("ticket.close")
async def ticket_close(ticket_id: TicketId) -> dict[str, Any]:
    """Close a resolved ticket.

    Args:
        ticket_id: Ticket ID with the TKT- prefix (opaque suffix).

    Returns:
        Updated ticket dict with ``state="closed"``.

    Raises:
        PolicyViolationFromServer:
            - ``ticket.close.must_be_resolved_first``.
    """
    return await get_clients().crm.close_ticket(ticket_id)


@register("ticket.cancel")
async def ticket_cancel(ticket_id: TicketId) -> dict[str, Any]:
    """Cancel a ticket before resolution. DESTRUCTIVE — ``safety.py`` blocks
    this without ``--allow-destructive``.

    Args:
        ticket_id: Ticket ID with the TKT- prefix (opaque suffix).

    Returns:
        Updated ticket dict with ``state="cancelled"``.

    Raises:
        PolicyViolationFromServer:
            - ``ticket.cancel.already_resolved``: already resolved — close instead.
    """
    return await get_clients().crm.cancel_ticket(ticket_id)
