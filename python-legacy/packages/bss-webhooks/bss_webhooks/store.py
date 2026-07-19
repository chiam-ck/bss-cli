"""Persistence helpers for the integrations schema.

Two thin async stores over SQLAlchemy 2.0 Core inserts:

* :class:`WebhookEventStore` — idempotent upsert into
  ``integrations.webhook_event`` keyed on ``(provider, event_id)``.
  Returns ``True`` on first insertion, ``False`` if the row already
  existed (provider retry).
* :class:`ExternalCallStore` — append-only insert into
  ``integrations.external_call``.

Both stores accept an :class:`AsyncSession` and run a single statement;
they do *not* manage transactions. The caller decides whether the
write commits with the surrounding domain write or stands alone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bss_clock import now as clock_now
from bss_models.integrations import ExternalCall, WebhookEvent
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


class WebhookEventStore:
    """Idempotent persistence for inbound provider webhooks."""

    async def persist(
        self,
        session: AsyncSession,
        *,
        provider: str,
        event_id: str,
        event_type: str,
        body: dict[str, Any],
        signature_valid: bool,
    ) -> bool:
        """Insert a webhook event, idempotent on (provider, event_id).

        :returns: ``True`` if a new row was inserted, ``False`` if the
            event was already known (provider retry, dedup at PK).
        """
        stmt = (
            pg_insert(WebhookEvent)
            .values(
                provider=provider,
                event_id=event_id,
                event_type=event_type,
                body=body,
                signature_valid=signature_valid,
            )
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
        )
        result = await session.execute(stmt)
        # rowcount == 1 → inserted; 0 → conflict (existing row).
        return bool(result.rowcount)

    async def mark_processed(
        self,
        session: AsyncSession,
        *,
        provider: str,
        event_id: str,
        outcome: str,
        error: str | None = None,
        processed_at: datetime | None = None,
    ) -> None:
        """Stamp processed_at + process_outcome on an existing webhook row."""
        row = await session.get(WebhookEvent, (provider, event_id))
        if row is None:
            raise LookupError(
                f"webhook_event ({provider!r}, {event_id!r}) not found; "
                "call persist() before mark_processed()"
            )
        row.process_outcome = outcome
        row.process_error = error
        if processed_at is not None:
            row.processed_at = processed_at
        else:
            row.processed_at = clock_now()


class ExternalCallStore:
    """Append-only forensic log for outbound provider calls."""

    async def record(
        self,
        session: AsyncSession,
        *,
        provider: str,
        operation: str,
        success: bool,
        latency_ms: int,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        provider_call_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        redacted_payload: dict[str, Any] | None = None,
        tenant_id: str = "DEFAULT",
    ) -> int:
        """Insert one row; return the assigned ``id``."""
        stmt = (
            pg_insert(ExternalCall)
            .values(
                provider=provider,
                operation=operation,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                success=success,
                latency_ms=latency_ms,
                provider_call_id=provider_call_id,
                error_code=error_code,
                error_message=error_message,
                redacted_payload=redacted_payload,
                tenant_id=tenant_id,
            )
            .returning(ExternalCall.id)
        )
        result = await session.execute(stmt)
        new_id: int = result.scalar_one()
        return new_id

    async def count_since(
        self,
        session: AsyncSession,
        *,
        provider: str,
        since: datetime,
    ) -> int:
        """Cheap counter for free-tier monitoring (Didit cap, etc.)."""
        stmt = select(ExternalCall.id).where(
            ExternalCall.provider == provider,
            ExternalCall.occurred_at >= since,
        )
        result = await session.execute(stmt)
        return len(result.all())
