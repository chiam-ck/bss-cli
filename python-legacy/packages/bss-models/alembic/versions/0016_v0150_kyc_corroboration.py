"""v0.15.0: KYC webhook corroboration + last-4 PII doctrine.

Two changes:

1. New ``integrations.kyc_webhook_corroboration`` table — every verified
   Didit webhook delivery records a row keyed by ``provider_session_id``.
   The CRM ``check_attestation_signature`` policy queries this table to
   verify a Didit attestation has a corroborating HMAC-verified webhook
   delivery before accepting. Trust anchor for v0.15+ KYC.
2. Two new columns on ``crm.customer_identity``:
   - ``document_number_last4`` (varchar(4), nullable for back-compat
     with pre-v0.15 rows; v0.15+ writes always populate)
   - ``corroboration_id`` (UUID, nullable; FK into
     ``integrations.kyc_webhook_corroboration.id``; populated when the
     attestation provider is ``didit``)

The two-column extension keeps existing v0.14 prebaked rows valid
without backfill — they have ``corroboration_id IS NULL`` because the
prebaked path doesn't go through Didit. The policy layer reads provider
to decide which form is required.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INTEGRATIONS_SCHEMA = "integrations"
CRM_SCHEMA = "crm"


def upgrade() -> None:
    op.create_table(
        "kyc_webhook_corroboration",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("provider_session_id", sa.Text, nullable=False),
        sa.Column("webhook_event_provider", sa.Text, nullable=False),
        sa.Column("webhook_event_id", sa.Text, nullable=False),
        sa.Column("decision_status", sa.Text, nullable=False),
        sa.Column("decision_body_digest", sa.Text, nullable=False),
        sa.Column(
            "received_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["webhook_event_provider", "webhook_event_id"],
            [
                f"{INTEGRATIONS_SCHEMA}.webhook_event.provider",
                f"{INTEGRATIONS_SCHEMA}.webhook_event.event_id",
            ],
        ),
        schema=INTEGRATIONS_SCHEMA,
    )
    op.create_index(
        "uq_kyc_corroboration_provider_session",
        "kyc_webhook_corroboration",
        ["provider", "provider_session_id"],
        unique=True,
        schema=INTEGRATIONS_SCHEMA,
    )

    op.add_column(
        "customer_identity",
        sa.Column("document_number_last4", sa.String(length=4), nullable=True),
        schema=CRM_SCHEMA,
    )
    op.add_column(
        "customer_identity",
        sa.Column(
            "corroboration_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{INTEGRATIONS_SCHEMA}.kyc_webhook_corroboration.id"
            ),
            nullable=True,
        ),
        schema=CRM_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(
        "customer_identity", "corroboration_id", schema=CRM_SCHEMA
    )
    op.drop_column(
        "customer_identity", "document_number_last4", schema=CRM_SCHEMA
    )
    op.drop_index(
        "uq_kyc_corroboration_provider_session",
        table_name="kyc_webhook_corroboration",
        schema=INTEGRATIONS_SCHEMA,
    )
    op.drop_table(
        "kyc_webhook_corroboration", schema=INTEGRATIONS_SCHEMA
    )
