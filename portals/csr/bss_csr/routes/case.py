"""Case thread drill-in — detail page + workbench (v1.6).

Per phases/V0_13_0.md §3.2, the case page is the one v0.5 surface kept
into v0.13: useful for copy-paste case-id deep links from a chat
session, slack, or a runbook. No login dependency anymore — the
cockpit runs single-operator-by-design behind a secure perimeter.

v1.6 — the read-only page grew the CRM workbench: notes, transitions,
priority, ticket lifecycle, all rendered from ``cases.workbench_context``
and POSTing to ``routes/cases.py``. The Case API speaks the internal
snake_case DTO (``customer_id``/``opened_at``), not TMF camelCase —
``views.field`` reads both, which also fixes the v0.13 page silently
blanking those fields.

Continues the v0.12 contract: when a case carries
``chat_transcript_hash`` (i.e. it was opened by the customer chat
surface via ``case.open_for_me``), the page renders a "Chat
transcript" panel below the notes. The transcript is fetched via
``CRMClient.get_chat_transcript``.
"""

from __future__ import annotations

import structlog
from bss_clients.errors import ClientError
from bss_cockpit import OPERATOR_ACTOR
from bss_cockpit import current as cockpit_config_current
from bss_orchestrator.clients import get_clients
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..templating import templates
from ..views import field, flatten_ticket, fmt_dt
from .cases import workbench_context

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/case/{case_id}", response_class=HTMLResponse)
async def case_thread(request: Request, case_id: str) -> HTMLResponse:
    clients = get_clients()
    try:
        case_raw = await clients.crm.get_case(case_id)
    except ClientError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
        raise

    tickets = case_raw.get("tickets") or []
    if not tickets:
        try:
            tickets = await clients.crm.list_tickets(case_id=case_id)
        except (ClientError, AttributeError):
            tickets = []
    ticket_views = [flatten_ticket(t) for t in tickets]

    # Agents for the assign dropdown — best-effort; an empty list just
    # hides the control.
    try:
        agents = [
            {"id": a.get("id", ""), "name": a.get("name", a.get("id", ""))}
            for a in await clients.crm.list_agents(state="active") or []
        ]
    except (ClientError, AttributeError):
        agents = []

    notes = sorted(
        case_raw.get("notes") or [],
        key=lambda n: str(field(n, "created_at", default="")),
    )

    transcript_hash = field(case_raw, "chat_transcript_hash", default=None)
    transcript_view: dict | None = None
    if transcript_hash:
        try:
            row = await clients.crm.get_chat_transcript(transcript_hash)
            transcript_view = {
                "hash": row.get("hash", transcript_hash),
                "body": row.get("body", ""),
                "recorded_at": row.get("recorded_at", ""),
            }
        except ClientError as exc:
            log.warning(
                "csr.case.chat_transcript_fetch_failed",
                case_id=case_id,
                hash=transcript_hash,
                status=getattr(exc, "status_code", None),
            )
            transcript_view = {
                "hash": transcript_hash,
                "body": None,
                "recorded_at": "",
                "error": "Transcript is no longer retrievable. It may have been archived.",
            }

    case_state = field(case_raw, "state", default="unknown")
    customer_id = field(case_raw, "customer_id", default="")

    cfg = cockpit_config_current()
    return templates.TemplateResponse(
        request,
        "case_thread.html",
        {
            "active_page": "cases",
            "actor": OPERATOR_ACTOR,
            "model": cfg.settings.llm.model or "(env default)",
            "case": {
                "id": case_raw.get("id", case_id),
                "subject": case_raw.get("subject", ""),
                "description": case_raw.get("description") or "",
                "state": case_state,
                "priority": field(case_raw, "priority", default=""),
                "category": field(case_raw, "category", default=""),
                "resolution_code": field(case_raw, "resolution_code", default=""),
                "agent_id": field(
                    case_raw, "opened_by_agent_id", "agent_id", "assigned_agent_id",
                    default="",
                ),
                "customer_id": customer_id,
                "created_at": fmt_dt(field(case_raw, "opened_at", "created_at", default="")),
                "closed_at": fmt_dt(field(case_raw, "closed_at", default="")),
                "chat_transcript_hash": transcript_hash,
            },
            "tickets": ticket_views,
            "agents": agents,
            "notes": [
                {
                    "id": n.get("id", ""),
                    "body": n.get("body", ""),
                    "author": field(n, "author_agent_id", "author", "created_by", default="system"),
                    "at": fmt_dt(field(n, "created_at", default="")),
                }
                for n in notes
            ],
            "transcript": transcript_view,
            "flash": request.query_params.get("flash", ""),
            "err": request.query_params.get("err", "")[:300],
            **workbench_context(case_state, ticket_views),
        },
    )
