"""ChatTranscript repository — append-only, hash-addressed (v0.12).

Used by ``case.open_for_me`` (orchestrator) → CRMClient.store_chat_transcript
→ CRM service. The CSR retrieves bodies via ``case.show_transcript_for``
(hash from crm.case.chat_transcript_hash → this repo's get).

Inserts are idempotent on the hash PK — re-storing the same hash is a
no-op so retries on transient errors don't blow up. (Re-storing a
*different* body for the same hash is a SHA-256 collision — left to
crash; the caller should never produce one.)
"""

from bss_models.audit import ChatTranscript
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


class ChatTranscriptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, hash_: str) -> ChatTranscript | None:
        stmt = select(ChatTranscript).where(ChatTranscript.hash == hash_)
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def insert_idempotent(
        self, *, hash_: str, customer_id: str, body: str
    ) -> ChatTranscript:
        """Insert the row if absent; if a row with this hash already
        exists, return it unchanged.

        Idempotency means the orchestrator can retry the
        store_chat_transcript HTTP call without producing a duplicate
        primary-key error. Same body → same hash → same row.
        """
        stmt = (
            pg_insert(ChatTranscript)
            .values(hash=hash_, customer_id=customer_id, body=body)
            .on_conflict_do_nothing(index_elements=["hash"])
        )
        await self._s.execute(stmt)
        await self._s.flush()
        # Re-read so the returned object has whichever values won
        # (existing row vs newly-inserted) — both are valid.
        existing = await self.get(hash_)
        assert existing is not None  # we either just inserted or it pre-existed
        return existing
