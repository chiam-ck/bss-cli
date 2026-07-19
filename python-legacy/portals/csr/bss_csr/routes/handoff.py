"""POST /cockpit/handoff — CRM screens hand work to the chat.

v1.6 doctrine: the CRM screens are supplementary views; the
conversation stays the canonical write path for anything destructive,
compound, or money-moving. Every "Ask the agent" button on a CRM page
POSTs here with an optional customer focus and a drafted message. We
open a fresh session (pinned to the customer when given) and land the
operator on the thread with the draft PREFILLED in the compose box —
never auto-sent. The operator reviews, edits, and presses Enter;
destructive verbs then ride the normal propose-then-``/confirm``
contract unchanged.
"""

from __future__ import annotations

from urllib.parse import urlencode

import structlog
from bss_cockpit import OPERATOR_ACTOR, Conversation
from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

log = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/cockpit/handoff", response_model=None)
async def cockpit_handoff(
    customer_id: str = Form(default=""),
    draft: str = Form(default=""),
    label: str = Form(default=""),
) -> RedirectResponse:
    customer_id = customer_id.strip()
    draft = draft.strip()
    conv = await Conversation.open(
        actor=OPERATOR_ACTOR,
        label=(label.strip() or None),
        customer_focus=(customer_id or None),
    )
    url = f"/cockpit/{conv.session_id}"
    if draft:
        url += "?" + urlencode({"draft": draft[:2000]})
    log.info(
        "cockpit.handoff",
        session_id=conv.session_id,
        customer_focus=customer_id or None,
        has_draft=bool(draft),
    )
    return RedirectResponse(url=url, status_code=303)
