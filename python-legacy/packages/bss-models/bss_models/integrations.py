"""Integrations schema — 2 tables.

The forensic substrate for real-provider integrations (v0.14+). Every
outbound provider call records to ``external_call``; every inbound
provider webhook persists to ``webhook_event`` (idempotent on
``(provider, event_id)``).

The ``audit.domain_event`` row remains the canonical event log; rows
that originated from a provider call gain an optional ``external_ref``
envelope on ``payload`` so forensic joins work without a schema change
to ``audit.domain_event``:

    payload->'external_ref'->>'id' ↔ integrations.external_call.provider_call_id
"""

from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TZDateTime

SCHEMA = "integrations"


class ExternalCall(Base):
    """Forensic per-call log for outbound provider HTTP requests.

    One row per attempted call (success or failure). The
    ``redacted_payload`` column stores the request/response envelope
    *after* per-provider redaction (``bss_webhooks.redaction``). Raw
    PII (PAN, NRIC, OTP) never lands here.
    """

    __tablename__ = "external_call"
    __table_args__ = (
        Index(
            "ix_external_call_aggregate",
            "aggregate_type",
            "aggregate_id",
        ),
        Index(
            "ix_external_call_provider_time",
            "provider",
            "occurred_at",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 'resend' (v0.14) | 'didit' (v0.15+) | 'stripe' (v0.16+) | 'sim_esim'
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # 'send_login' | 'send_step_up' | 'charge' | 'fetch_attestation' | …
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    # 'identity' | 'session' | 'payment_attempt' | None for fire-and-forget
    aggregate_type: Mapped[str | None] = mapped_column(Text)
    # IDT-* | SES-* | ATT-* | None
    aggregate_id: Mapped[str | None] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # Provider's own id (Resend msg_*, Stripe pi_*, Didit session_id).
    provider_call_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    redacted_payload: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    tenant_id: Mapped[str] = mapped_column(
        Text, nullable=False, default="DEFAULT", server_default="DEFAULT"
    )


class WebhookEvent(Base):
    """Inbound provider webhook log, idempotent on ``(provider, event_id)``.

    Every webhook receiver persists here on first sight. Composite PK
    naturally dedupes provider retries (Resend, Stripe both use
    at-least-once delivery). ``signature_valid=False`` rows are kept —
    they represent attempted-tampering or misconfigured-secret cases
    that ops needs to see, not silently dropped.
    """

    __tablename__ = "webhook_event"
    __table_args__ = (
        Index(
            "ix_webhook_event_received_unprocessed",
            "received_at",
            postgresql_where="processed_at IS NULL",
        ),
        {"schema": SCHEMA},
    )

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # 'email.delivered' | 'charge.succeeded' | …
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # 'reconciled' | 'noop' | 'errored'
    process_outcome: Mapped[str | None] = mapped_column(Text)
    process_error: Mapped[str | None] = mapped_column(Text)


class KycWebhookCorroboration(Base):
    """Verified-record-of-truth for a KYC verification session (v0.15+).

    Written by the ``/webhooks/didit`` route handler after
    ``bss_webhooks.signatures.verify_signature(scheme="didit_hmac", …)``
    succeeds. The CRM ``check_attestation_signature`` policy queries this
    table by ``(provider, provider_session_id)`` to verify a Didit
    attestation has a corroborating HMAC-verified webhook delivery.

    The trust anchor for v0.15 KYC: the Didit decision API returns plain
    JSON over TLS with no signature, so the API response alone cannot be
    trusted. The webhook (HMAC-signed) is the only authenticated channel.
    """

    __tablename__ = "kyc_webhook_corroboration"
    __table_args__ = (
        Index(
            "uq_kyc_corroboration_provider_session",
            "provider",
            "provider_session_id",
            unique=True,
        ),
        ForeignKeyConstraint(
            ["webhook_event_provider", "webhook_event_id"],
            [f"{SCHEMA}.webhook_event.provider", f"{SCHEMA}.webhook_event.event_id"],
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(
        Text, nullable=False, default="DEFAULT", server_default="DEFAULT"
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_event_provider: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 'Approved' | 'Declined' | 'Expired' | 'In Review'
    decision_status: Mapped[str] = mapped_column(Text, nullable=False)
    decision_body_digest: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
