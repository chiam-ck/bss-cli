"""ChatTranscript DTOs (v0.12).

The transcript itself is treated as opaque text — we don't parse it
on the CRM side. The hash is content-addressed (SHA-256 of body) and
the orchestrator computes it before posting; the service stores
hash + customer_id + body verbatim.
"""

from datetime import datetime

from pydantic import BaseModel


class StoreChatTranscriptRequest(BaseModel):
    hash: str
    customer_id: str
    body: str


class ChatTranscriptResponse(BaseModel):
    hash: str
    customer_id: str
    body: str
    recorded_at: datetime


def to_chat_transcript_response(t) -> ChatTranscriptResponse:
    return ChatTranscriptResponse(
        hash=t.hash,
        customer_id=t.customer_id,
        body=t.body,
        recorded_at=t.recorded_at,
    )
