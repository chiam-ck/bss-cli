"""v0.12.0 PR 1: chat-surface scoping schema.

Three operations, all in service of v0.12's chat-scoping doctrine:

* ``audit.chat_usage`` — per-customer per-month rate + cost counters.
  Composite PK on ``(customer_id, period_yyyymm)``. Incremented by
  ``chat_caps.record_chat_turn`` (PR5). Read by ``check_caps`` to
  enforce the monthly cost cap.

* ``audit.chat_transcript`` — content-addressed transcript storage.
  PK is the SHA-256 hash of the transcript body. Append-only.
  Cases reference the hash; CSR retrieval reads ``body`` by hash.

* ``crm.case.chat_transcript_hash`` — nullable FK-shaped column on
  the existing case table, populated when a case is opened by
  ``case.open_for_me`` from a chat-surface escalation. NULL for
  cases opened via the CSR/CLI path.

Both new tables live in the existing ``audit`` schema (already
created in 0001_initial). No CREATE SCHEMA needed.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AUDIT_SCHEMA = "audit"
CRM_SCHEMA = "crm"


def upgrade() -> None:
    op.create_table(
        "chat_usage",
        sa.Column("customer_id", sa.Text, primary_key=True, nullable=False),
        sa.Column("period_yyyymm", sa.Integer, primary_key=True, nullable=False),
        sa.Column(
            "requests_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cost_cents",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "last_updated",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=AUDIT_SCHEMA,
    )

    op.create_table(
        "chat_transcript",
        sa.Column("hash", sa.Text, primary_key=True, nullable=False),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column(
            "recorded_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=AUDIT_SCHEMA,
    )
    op.create_index(
        "ix_chat_transcript_customer",
        "chat_transcript",
        ["customer_id", "recorded_at"],
        schema=AUDIT_SCHEMA,
    )

    op.add_column(
        "case",
        sa.Column("chat_transcript_hash", sa.Text, nullable=True),
        schema=CRM_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("case", "chat_transcript_hash", schema=CRM_SCHEMA)
    op.drop_index(
        "ix_chat_transcript_customer",
        table_name="chat_transcript",
        schema=AUDIT_SCHEMA,
    )
    op.drop_table("chat_transcript", schema=AUDIT_SCHEMA)
    op.drop_table("chat_usage", schema=AUDIT_SCHEMA)
