"""Chat transcript service — store + retrieve, hash-addressed (v0.12).

Thin orchestrator over ``ChatTranscriptRepository``. No policy gating
beyond the existing perimeter (BSS_API_TOKEN); transcripts are
treated as audit data — the chat surface is the only writer in v0.12,
and CSR is the only reader (via case.show_transcript_for).
"""

from __future__ import annotations

import hashlib

import structlog
from bss_models.audit import ChatTranscript
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.chat_transcript_repo import ChatTranscriptRepository

log = structlog.get_logger()


def expected_hash_for(body: str) -> str:
    """Compute the canonical SHA-256 hash for a transcript body. The
    orchestrator and the CRM service must agree on this — a mismatch
    would break the case → transcript join."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ChatTranscriptService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        chat_transcript_repo: ChatTranscriptRepository,
    ) -> None:
        self._session = session
        self._repo = chat_transcript_repo

    async def store(
        self, *, hash_: str, customer_id: str, body: str
    ) -> ChatTranscript:
        """Idempotent insert. The orchestrator computes the hash and
        passes it; we re-compute and reject mismatches so the column
        cannot be poisoned with a body that does not match its key.
        """
        actual = expected_hash_for(body)
        if actual != hash_:
            from app.policies.base import PolicyViolation

            raise PolicyViolation(
                rule="chat_transcript.hash_mismatch",
                message="Provided hash does not match SHA-256 of body.",
                context={"provided": hash_, "expected": actual},
            )
        row = await self._repo.insert_idempotent(
            hash_=hash_, customer_id=customer_id, body=body
        )
        await self._session.commit()
        return row

    async def get(self, hash_: str) -> ChatTranscript | None:
        return await self._repo.get(hash_)
