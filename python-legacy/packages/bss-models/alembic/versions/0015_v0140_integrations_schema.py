"""v0.14.0: integrations schema (2 tables) for real-provider forensic substrate.

Two tables in a new ``integrations`` schema:

* ``integrations.external_call`` — append-only log of every outbound
  provider HTTP call. ``redacted_payload`` stores the request/response
  envelope post-redaction (``bss_webhooks.redaction``); raw PII never
  lands here.
* ``integrations.webhook_event`` — inbound provider webhooks,
  idempotent on ``(provider, event_id)`` PK. Provider retries dedupe
  naturally; signature-invalid rows are kept for ops visibility.

The schema is open enum on ``provider`` so v0.14 (resend) extends to
v0.15 (didit) and v0.16 (stripe) without migration. No
``provider_config`` table — config lives in ``.env`` only. DB-backed
multi-tenant config is post-v0.16.

The ``audit.domain_event.payload`` envelope ``external_ref`` (provider,
operation, id, idempotency_key) is doctrine, not schema — no
migration needed there.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "integrations"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    # ── external_call ────────────────────────────────────────────────
    op.create_table(
        "external_call",
        sa.Column(
            "id", sa.BigInteger, primary_key=True, autoincrement=True
        ),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("aggregate_type", sa.Text, nullable=True),
        sa.Column("aggregate_id", sa.Text, nullable=True),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("provider_call_id", sa.Text, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "redacted_payload",
            sa.dialects.postgresql.JSONB,
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    # Forensic: list every call against a given aggregate.
    op.create_index(
        "ix_external_call_aggregate",
        "external_call",
        ["aggregate_type", "aggregate_id"],
        schema=SCHEMA,
    )
    # Per-provider time-window queries (free-tier monitoring,
    # `bss external-calls --provider <p> --since <window>`).
    op.create_index(
        "ix_external_call_provider_time",
        "external_call",
        ["provider", "occurred_at"],
        schema=SCHEMA,
    )

    # ── webhook_event ────────────────────────────────────────────────
    op.create_table(
        "webhook_event",
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column(
            "body",
            sa.dialects.postgresql.JSONB,
            nullable=False,
        ),
        sa.Column("signature_valid", sa.Boolean, nullable=False),
        sa.Column(
            "received_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("process_outcome", sa.Text, nullable=True),
        sa.Column("process_error", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("provider", "event_id"),
        schema=SCHEMA,
    )
    # Find unprocessed webhooks for replay/diagnostic flows.
    op.create_index(
        "ix_webhook_event_received_unprocessed",
        "webhook_event",
        ["received_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("processed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_event_received_unprocessed",
        table_name="webhook_event",
        schema=SCHEMA,
    )
    op.drop_table("webhook_event", schema=SCHEMA)

    op.drop_index(
        "ix_external_call_provider_time",
        table_name="external_call",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_external_call_aggregate",
        table_name="external_call",
        schema=SCHEMA,
    )
    op.drop_table("external_call", schema=SCHEMA)

    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
