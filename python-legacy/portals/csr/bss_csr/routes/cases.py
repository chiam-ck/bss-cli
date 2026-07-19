"""Case queue + workbench actions (v1.6 cockpit CRM).

The queue is a read view over ``crm.list_cases``. The workbench POST
routes cover the case/ticket verbs — note, transition, priority,
open/assign/resolve/close ticket, case close, ticket cancel — each a
single policy-gated ``bss-clients`` call; ``PolicyViolation`` messages
flash back onto the case page verbatim (they are operator-facing copy
already).

v1.6.1 (operator directive) — destructive verbs are direct CRUD with a
**two-step UI confirm**: the form must carry ``confirm=yes`` (rendered
by the expanded ``crm-danger-form`` panel) or the route refuses. The
human clicking through the expanded consequence IS the authorisation;
the policy layer stays the server-side gate. The LLM path keeps its own
propose-then-``/confirm`` contract unchanged, and "Ask the agent"
handoffs remain available for narrative/compound work.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import structlog
from bss_clients.errors import ClientError, PolicyViolationFromServer
from bss_orchestrator.clients import get_clients
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..templating import templates
from ..views import flatten_case

log = structlog.get_logger(__name__)
router = APIRouter()

PAGE_SIZE = 25

CASE_STATES = ["open", "in_progress", "pending_customer", "resolved", "closed"]

# Case-state → workbench transition buttons (label, trigger). Mirrors
# services/crm/app/domain/case_state.py — keep in sync when the FSM
# grows; an invalid trigger degrades gracefully to a PolicyViolation
# flash, never a 500.
CASE_ACTIONS: dict[str, list[tuple[str, str]]] = {
    "open": [("Take", "take"), ("Resolve", "resolve")],
    "in_progress": [("Await customer", "await_customer"), ("Resolve", "resolve")],
    "pending_customer": [("Resume", "resume")],
    "resolved": [],
    "closed": [],
}

# Ticket-state → (label, target state for transition_ticket). ``resolve``
# is handled by its own form (resolution notes are required).
TICKET_ACTIONS: dict[str, list[tuple[str, str]]] = {
    "open": [("Acknowledge", "acknowledged")],
    "acknowledged": [("Start", "in_progress")],
    "in_progress": [],
    "pending": [("Resume", "in_progress")],
    "resolved": [("Close", "closed"), ("Reopen", "in_progress")],
    "closed": [],
    "cancelled": [],
}

TICKET_TYPES = ["information_request", "technical", "subscription", "billing"]
PRIORITIES = ["low", "normal", "medium", "high", "critical"]


@router.get("/cases", response_class=HTMLResponse)
async def cases_list(
    request: Request,
    state: str = "",
    customer: str = "",
    page: int = Query(default=0, ge=0, le=10_000),
) -> HTMLResponse:
    state_clean = state.strip()
    customer_clean = customer.strip()
    clients = get_clients()
    try:
        raw = await clients.crm.list_cases(
            customer_id=customer_clean or None,
            state=state_clean or None,
            limit=PAGE_SIZE + 1,
            offset=page * PAGE_SIZE,
        )
    except ClientError as exc:
        log.warning("csr.cases.list_failed", status=exc.status_code)
        raw = []
    has_next = len(raw or []) > PAGE_SIZE
    rows = [flatten_case(c) for c in (raw or [])[:PAGE_SIZE]]

    return templates.TemplateResponse(
        request,
        "cases_list.html",
        {
            "active_page": "cases",
            "model": "(env default)",
            "state": state_clean,
            "customer": customer_clean,
            "states": CASE_STATES,
            "rows": rows,
            "page": page,
            "has_prev": page > 0,
            "has_next": has_next,
        },
    )


def _back_to_case(case_id: str, **params: str) -> RedirectResponse:
    url = f"/case/{case_id}"
    filtered = {k: v for k, v in params.items() if v}
    if filtered:
        url += "?" + urlencode(filtered)
    return RedirectResponse(url=url, status_code=303)


async def _run(case_id: str, action: str, coro) -> RedirectResponse:
    """Run one write; flash the outcome back onto the case page."""
    try:
        await coro
    except PolicyViolationFromServer as exc:
        return _back_to_case(case_id, err=exc.detail)
    except ClientError as exc:
        return _back_to_case(case_id, err=f"CRM error ({exc.status_code})")
    except ValueError as exc:
        # bss-clients raises ValueError on transitions the state map
        # can't express from the current state — operator-facing copy.
        return _back_to_case(case_id, err=str(exc))
    return _back_to_case(case_id, flash=action)


CONFIRM_REQUIRED = "This action needs the expanded confirm step."


@router.post("/case/{case_id}/close", response_model=None)
async def case_close(
    case_id: str,
    resolution_code: str = Form(...),
    confirm: str = Form(default=""),
) -> RedirectResponse:
    if confirm != "yes":
        return _back_to_case(case_id, err=CONFIRM_REQUIRED)
    return await _run(
        case_id, "case_closed",
        get_clients().crm.close_case(
            case_id, resolution_code=resolution_code.strip()
        ),
    )


@router.post("/case/{case_id}/ticket/{ticket_id}/cancel", response_model=None)
async def ticket_cancel(
    case_id: str, ticket_id: str, confirm: str = Form(default="")
) -> RedirectResponse:
    if confirm != "yes":
        return _back_to_case(case_id, err=CONFIRM_REQUIRED)
    return await _run(
        case_id, "ticket_cancelled",
        get_clients().crm.cancel_ticket(ticket_id),
    )


@router.post("/case/{case_id}/note", response_model=None)
async def case_add_note(
    case_id: str, body: str = Form(...)
) -> RedirectResponse:
    return await _run(
        case_id, "note_added",
        get_clients().crm.add_case_note(case_id, body=body.strip()),
    )


@router.post("/case/{case_id}/transition", response_model=None)
async def case_transition(
    case_id: str, trigger: str = Form(...)
) -> RedirectResponse:
    valid = {t for actions in CASE_ACTIONS.values() for _, t in actions}
    if trigger not in valid:
        return _back_to_case(case_id, err=f"Unknown transition {trigger!r}")
    return await _run(
        case_id, "transitioned",
        get_clients().crm.transition_case(case_id, trigger=trigger),
    )


@router.post("/case/{case_id}/priority", response_model=None)
async def case_priority(
    case_id: str, priority: str = Form(...)
) -> RedirectResponse:
    return await _run(
        case_id, "priority_updated",
        get_clients().crm.update_case_priority(case_id, priority=priority),
    )


@router.post("/case/{case_id}/ticket", response_model=None)
async def case_open_ticket(
    case_id: str,
    customer_id: str = Form(...),
    ticket_type: str = Form(default="information_request"),
    subject: str = Form(...),
) -> RedirectResponse:
    ttype = ticket_type if ticket_type in TICKET_TYPES else "information_request"
    return await _run(
        case_id, "ticket_opened",
        get_clients().crm.open_ticket(
            ticket_type=ttype,
            subject=subject.strip(),
            case_id=case_id,
            customer_id=customer_id,
        ),
    )


@router.post("/case/{case_id}/ticket/{ticket_id}/assign", response_model=None)
async def ticket_assign(
    case_id: str, ticket_id: str, agent_id: str = Form(...)
) -> RedirectResponse:
    return await _run(
        case_id, "ticket_assigned",
        get_clients().crm.assign_ticket(ticket_id, agent_id=agent_id),
    )


@router.post("/case/{case_id}/ticket/{ticket_id}/transition", response_model=None)
async def ticket_transition(
    case_id: str, ticket_id: str, to_state: str = Form(...)
) -> RedirectResponse:
    valid = {s for actions in TICKET_ACTIONS.values() for _, s in actions}
    if to_state not in valid:
        return _back_to_case(case_id, err=f"Unknown ticket transition {to_state!r}")
    return await _run(
        case_id, "ticket_transitioned",
        get_clients().crm.transition_ticket(ticket_id, to_state=to_state),
    )


@router.post("/case/{case_id}/ticket/{ticket_id}/resolve", response_model=None)
async def ticket_resolve(
    case_id: str, ticket_id: str, resolution_notes: str = Form(...)
) -> RedirectResponse:
    return await _run(
        case_id, "ticket_resolved",
        get_clients().crm.resolve_ticket(
            ticket_id, resolution_notes=resolution_notes.strip()
        ),
    )


def workbench_context(case_state: str, tickets: list[dict[str, Any]]) -> dict[str, Any]:
    """Action availability for the case page template (used by case.py)."""
    return {
        "case_actions": CASE_ACTIONS.get(case_state, []),
        "ticket_actions_by_id": {
            t["id"]: TICKET_ACTIONS.get(str(t.get("state", "")), [])
            for t in tickets
        },
        "ticket_types": TICKET_TYPES,
        "priorities": PRIORITIES,
        "case_is_open": case_state not in ("closed",),
    }
