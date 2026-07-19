"""Custom Case endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_case_service
from app.schemas.internal.case import (
    AddNoteRequest,
    CaseNoteResponse,
    CaseResponse,
    CloseCaseRequest,
    OpenCaseRequest,
    PatchCaseRequest,
    to_case_note_response,
    to_case_response,
)
from app.services.case_service import CaseService

router = APIRouter(tags=["Case"])


@router.post("/case", response_model=CaseResponse, status_code=201)
async def open_case(
    body: OpenCaseRequest,
    svc: CaseService = Depends(get_case_service),
) -> CaseResponse:
    case = await svc.open_case(
        customer_id=body.customer_id,
        subject=body.subject,
        description=body.description,
        priority=body.priority,
        category=body.category,
        opened_by_agent_id=body.opened_by_agent_id,
        chat_transcript_hash=body.chat_transcript_hash,
    )
    case = await svc.get_case(case.id)
    return to_case_response(case)


@router.get("/case", response_model=list[CaseResponse])
async def list_cases(
    customerId: str | None = None,
    state: str | None = None,
    assignedAgentId: str | None = None,
    limit: int = 20,
    offset: int = 0,
    svc: CaseService = Depends(get_case_service),
) -> list[CaseResponse]:
    cases = await svc.list_cases(
        customer_id=customerId,
        state=state,
        assigned_agent_id=assignedAgentId,
        limit=limit,
        offset=offset,
    )
    return [to_case_response(c) for c in cases]


@router.get("/case/{case_id}", response_model=CaseResponse)
async def get_case(
    case_id: str,
    svc: CaseService = Depends(get_case_service),
) -> CaseResponse:
    case = await svc.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    return to_case_response(case)


@router.patch("/case/{case_id}", response_model=CaseResponse)
async def update_case(
    case_id: str,
    body: PatchCaseRequest,
    svc: CaseService = Depends(get_case_service),
) -> CaseResponse:
    if body.trigger:
        case = await svc.transition_case(
            case_id,
            body.trigger,
            resolution_code=body.resolution_code,
        )
    if body.priority is not None or body.category is not None:
        case = await svc.update_case_fields(
            case_id, priority=body.priority, category=body.category
        )
    if not body.trigger and body.priority is None and body.category is None:
        raise HTTPException(
            status_code=400,
            detail="PATCH body must carry a trigger and/or priority/category",
        )
    case = await svc.get_case(case_id)
    return to_case_response(case)


@router.post("/case/{case_id}/close", response_model=CaseResponse)
async def close_case(
    case_id: str,
    body: CloseCaseRequest,
    svc: CaseService = Depends(get_case_service),
) -> CaseResponse:
    case = await svc.close_case(case_id, resolution_code=body.resolution_code)
    case = await svc.get_case(case.id)
    return to_case_response(case)


@router.post("/case/{case_id}/note", response_model=CaseNoteResponse, status_code=201)
async def add_note(
    case_id: str,
    body: AddNoteRequest,
    svc: CaseService = Depends(get_case_service),
) -> CaseNoteResponse:
    note = await svc.add_note(
        case_id, body=body.body, author_agent_id=body.author_agent_id
    )
    return to_case_note_response(note)
