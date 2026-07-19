"""Chat transcript endpoints (v0.12).

Two routes:

* ``POST /crm-api/v1/chat-transcript`` — orchestrator's
  ``case.open_for_me`` posts here before opening the case.
  Idempotent on the hash PK.

* ``GET /crm-api/v1/chat-transcript/{hash}`` — CSR's
  ``case.show_transcript_for`` reads through here. 404 when the
  hash is unknown.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_chat_transcript_service
from app.schemas.internal.chat_transcript import (
    ChatTranscriptResponse,
    StoreChatTranscriptRequest,
    to_chat_transcript_response,
)
from app.services.chat_transcript_service import ChatTranscriptService

router = APIRouter(tags=["ChatTranscript"])


@router.post(
    "/chat-transcript",
    response_model=ChatTranscriptResponse,
    status_code=201,
)
async def store_chat_transcript(
    body: StoreChatTranscriptRequest,
    svc: ChatTranscriptService = Depends(get_chat_transcript_service),
) -> ChatTranscriptResponse:
    row = await svc.store(
        hash_=body.hash,
        customer_id=body.customer_id,
        body=body.body,
    )
    return to_chat_transcript_response(row)


@router.get(
    "/chat-transcript/{hash_}",
    response_model=ChatTranscriptResponse,
)
async def get_chat_transcript(
    hash_: str,
    svc: ChatTranscriptService = Depends(get_chat_transcript_service),
) -> ChatTranscriptResponse:
    row = await svc.get(hash_)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"chat transcript {hash_} not found"
        )
    return to_chat_transcript_response(row)
