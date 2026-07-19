"""Internal Case + CaseNote DTOs (snake_case)."""

from datetime import datetime

from pydantic import BaseModel


class CaseResponse(BaseModel):
    id: str
    customer_id: str
    subject: str
    description: str | None = None
    state: str
    priority: str | None = None
    category: str | None = None
    resolution_code: str | None = None
    opened_by_agent_id: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    notes: list["CaseNoteResponse"] = []
    ticket_ids: list[str] = []
    # v0.12 — set when case.open_for_me opens the case from the chat
    # surface; references audit.chat_transcript.hash. NULL otherwise.
    chat_transcript_hash: str | None = None


class CaseNoteResponse(BaseModel):
    id: str
    case_id: str
    author_agent_id: str | None = None
    body: str
    created_at: datetime


class OpenCaseRequest(BaseModel):
    customer_id: str
    subject: str
    description: str | None = None
    priority: str = "normal"
    category: str = "general"
    opened_by_agent_id: str | None = None
    # v0.12 — set by case.open_for_me when the case is opened from
    # the chat surface. The transcript itself must already have been
    # POSTed to /crm-api/v1/chat-transcript before this call so the
    # hash resolves on retrieval.
    chat_transcript_hash: str | None = None


class UpdateCaseRequest(BaseModel):
    priority: str | None = None
    category: str | None = None


class CloseCaseRequest(BaseModel):
    resolution_code: str


class TransitionCaseRequest(BaseModel):
    trigger: str
    resolution_code: str | None = None


class PatchCaseRequest(BaseModel):
    """Unified PATCH body — a transition (trigger) and/or field updates.

    v1.6 — the endpoint previously deserialized ``TransitionCaseRequest``
    only, so ``{"priority": ...}`` PATCHes (the bss-clients
    ``update_case_priority`` shape since v0.13) failed validation before
    reaching the service.
    """

    trigger: str | None = None
    resolution_code: str | None = None
    priority: str | None = None
    category: str | None = None


class AddNoteRequest(BaseModel):
    body: str
    author_agent_id: str | None = None


def to_case_response(c) -> CaseResponse:
    return CaseResponse(
        id=c.id,
        customer_id=c.customer_id,
        subject=c.subject,
        description=c.description,
        state=c.state,
        priority=c.priority,
        category=c.category,
        resolution_code=c.resolution_code,
        opened_by_agent_id=c.opened_by_agent_id,
        opened_at=c.opened_at,
        closed_at=c.closed_at,
        notes=[
            CaseNoteResponse(
                id=n.id,
                case_id=n.case_id,
                author_agent_id=n.author_agent_id,
                body=n.body,
                created_at=n.created_at,
            )
            for n in (c.notes or [])
        ],
        ticket_ids=[t.id for t in (c.tickets or [])],
        chat_transcript_hash=getattr(c, "chat_transcript_hash", None),
    )


def to_case_note_response(n) -> CaseNoteResponse:
    return CaseNoteResponse(
        id=n.id,
        case_id=n.case_id,
        author_agent_id=n.author_agent_id,
        body=n.body,
        created_at=n.created_at,
    )
