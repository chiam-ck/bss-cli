"""Case tools — the CRM Case aggregate (ServiceNow-shaped, parents tickets)."""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import (
    AgentId,
    CaseCategory,
    CaseId,
    CasePriority,
    CaseState,
    CustomerId,
)
from ._registry import register


@register("case.open")
async def case_open(
    customer_id: CustomerId,
    subject: str,
    category: CaseCategory,
    priority: CasePriority,
) -> dict[str, Any]:
    """Open a new support case for a customer. Use this when the user reports
    an issue that is either (a) not clearly a single technical incident, or
    (b) likely to spawn multiple trouble tickets. Otherwise consider
    ``ticket.open`` directly.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        subject: Short description, e.g. ``"Data stopped working this morning"``.
        category: One of ``technical``, ``billing``, ``account``, ``information``.
        priority: One of ``low``, ``medium``, ``high``, ``critical``.

    Returns:
        Case dict ``{id, customerId, subject, category, priority, state, createdAt}``.
        Pass ``id`` to ``ticket.open`` as ``case_id`` to attach tickets.

    Raises:
        PolicyViolationFromServer:
            - ``case.open.customer_must_be_active``: open the case against a
              different customer, or activate the customer first.
    """
    return await get_clients().crm.open_case(
        customer_id=customer_id, subject=subject, category=category, priority=priority
    )


@register("case.get")
async def case_get(case_id: CaseId) -> dict[str, Any]:
    """Get a case with its notes + child-ticket IDs.

    Args:
        case_id: Case ID with the CASE- prefix (opaque suffix).

    Returns:
        Case dict. Use ``ticket.list(case_id=...)`` to fetch full ticket
        objects — this call returns only IDs to keep the payload small.

    Raises:
        NotFound: no case with this ID.
    """
    return await get_clients().crm.get_case(case_id)


@register("case.list")
async def case_list(
    customer_id: CustomerId | None = None,
    state: CaseState | None = None,
    agent_id: AgentId | None = None,
) -> list[dict[str, Any]]:
    """List cases, optionally filtered. At least one filter recommended — the
    unfiltered list can be long.

    Args:
        customer_id: Filter by customer.
        state: Filter by case state.
        agent_id: Filter by assigned agent.

    Returns:
        List of case dicts. May be empty.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().crm.list_cases(
        customer_id=customer_id, state=state, agent_id=agent_id
    )


@register("case.add_note")
async def case_add_note(case_id: CaseId, body: str) -> dict[str, Any]:
    """Append an internal note to a case.

    Args:
        case_id: Case ID with the CASE- prefix (opaque suffix).
        body: Note text (free text).

    Returns:
        The created note ``{id, caseId, authorId, createdAt, body}``.

    Raises:
        PolicyViolationFromServer: ``case.add_note.case_must_be_open``.
    """
    return await get_clients().crm.add_case_note(case_id, body=body)


@register("case.update_priority")
async def case_update_priority(
    case_id: CaseId, priority: CasePriority
) -> dict[str, Any]:
    """Update the priority of a case.

    Args:
        case_id: Case ID with the CASE- prefix (opaque suffix).
        priority: New priority.

    Returns:
        The updated case dict.

    Raises:
        (none common)
    """
    return await get_clients().crm.update_case_priority(case_id, priority=priority)


@register("case.transition")
async def case_transition(case_id: CaseId, to_state: CaseState) -> dict[str, Any]:
    """Explicitly transition a case to a new state. Prefer ``case.close``
    for the closing transition — it enforces extra policy.

    Args:
        case_id: Case ID with the CASE- prefix (opaque suffix).
        to_state: Target state.

    Returns:
        The updated case dict.

    Raises:
        PolicyViolationFromServer:
            - ``case.transition.illegal``: state machine rejected this move.
    """
    return await get_clients().crm.transition_case(case_id, to_state=to_state)


@register("case.show_transcript_for")
async def case_show_transcript_for(case_id: CaseId) -> dict[str, Any]:
    """Retrieve the chat transcript linked to a case (if any).

    v0.12 — used by the CSR console's case-detail page to show the
    conversation that led to an AI-opened escalation. Not in the
    customer chat profile (CSRs are the audience).

    Args:
        case_id: Case ID with the CASE- prefix (opaque suffix).

    Returns:
        ``{"transcript": <body>, "customerId": ..., "recordedAt": ...}``
        when the case has a chat_transcript_hash and the transcript
        row exists. ``{"transcript": null, "reason":
        "no_transcript_linked"}`` when the case carries no hash
        (CSR-opened, scenario-opened, or pre-v0.12 case).

    Raises:
        NotFound: the case has a hash but the transcript row is
            missing — investigate (transcript archived, hash
            corrupted, or DB inconsistency).
    """
    case = await get_clients().crm.get_case(case_id)
    transcript_hash = case.get("chatTranscriptHash") or case.get(
        "chat_transcript_hash"
    )
    if not transcript_hash:
        return {"transcript": None, "reason": "no_transcript_linked"}
    return await get_clients().crm.get_chat_transcript(transcript_hash)


@register("case.close")
async def case_close(case_id: CaseId, resolution_code: str) -> dict[str, Any]:
    """Close a case with a resolution code. Requires all child tickets to be
    resolved or closed first.

    Args:
        case_id: Case ID with the CASE- prefix (opaque suffix).
        resolution_code: Short resolution slug, e.g. ``"fixed"``, ``"duplicate"``.

    Returns:
        The updated case dict with ``state="closed"``.

    Raises:
        PolicyViolationFromServer:
            - ``case.close.requires_all_tickets_resolved``: resolve the listed
              open tickets first. ``context.open_tickets`` gives the IDs.
    """
    return await get_clients().crm.close_case(case_id, resolution_code=resolution_code)
