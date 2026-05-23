"""Audit schema — 3 tables.

domain_event (outbox + replay substrate).
chat_usage (v0.12 — per-customer monthly rate + cost counters).
chat_transcript (v0.12 — content-addressed transcripts for escalation).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, Index, Integer, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TZDateTime

SCHEMA = "audit"


class DomainEvent(Base):
    __tablename__ = "domain_event"
    __table_args__ = (
        Index(
            "ix_domain_event_aggregate_replay",
            "aggregate_type",
            "aggregate_id",
            "occurred_at",
        ),
        Index(
            "ix_domain_event_type_time",
            "event_type",
            "occurred_at",
        ),
        Index(
            "ix_domain_event_unpublished",
            "published_to_mq",
            postgresql_where="NOT published_to_mq",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, default="DEFAULT", server_default="DEFAULT")
    # v0.9 — resolved at the BSS perimeter from validated token (never
    # from a separate header). "default" / "portal_self_serve" /
    # "partner_<name>". Backfilled to "default" for pre-v0.9 rows.
    service_identity: Mapped[str] = mapped_column(
        Text, nullable=False, default="default", server_default="default"
    )
    payload: Mapped[dict | None] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    published_to_mq: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # v1.2 — outbox relay bookkeeping. The relay (bss_events.relay) increments
    # published_attempts on each delivery try and records last_publish_error, so a
    # row that won't publish is visible to triage instead of silently stuck.
    published_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    last_publish_error: Mapped[str | None] = mapped_column(Text)


class ChatUsage(Base):
    """v0.12 — per-customer per-month chat counters.

    One row per (customer, period_yyyymm). Incremented atomically by
    chat_caps.record_chat_turn after each completed turn. Used by
    check_caps to enforce the monthly cost cap; the hourly rate cap
    is in-memory and not stored here.
    """

    __tablename__ = "chat_usage"
    __table_args__ = {"schema": SCHEMA}

    customer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    period_yyyymm: Mapped[int] = mapped_column(Integer, primary_key=True)
    requests_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_updated: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )


class ChatTranscript(Base):
    """v0.12 — content-addressed transcript storage.

    A chat transcript is hashed (SHA-256 of the concatenated turn
    bodies) and persisted once; cases reference the hash via
    crm.case.chat_transcript_hash. Append-only.
    """

    __tablename__ = "chat_transcript"
    __table_args__ = {"schema": SCHEMA}

    hash: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
