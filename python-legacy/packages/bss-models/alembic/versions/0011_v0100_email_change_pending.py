"""v0.10.0 PR 8: portal_auth.email_change_pending — pending email-change verifications.

Tracks an in-flight email-change request between the
``start_email_change`` POST and the ``verify_email_change`` POST.
The customer enters their new email, receives an OTP at the *new*
address, and clicks/enters the code to commit the change. The
pending row is the single point that lets us reconcile a half-flow
(customer started a change but never verified) and enforce the 24h
expiry window.

Schema:

* ``id``           — text, identity_id-prefixed (``ECP-...``).
* ``identity_id``  — FK to ``portal_auth.identity.id``. Each identity
                     may have at most one active pending row at a time
                     (enforced via partial unique index). Re-starting
                     the flow voids the prior pending row.
* ``new_email``    — the proposed new email value. Captured here so
                     a concurrent change to the identity row doesn't
                     race with verification.
* ``code_hash``    — HMAC-SHA-256 of the OTP, salted with the server
                     pepper. Same hashing primitive as ``login_token``.
* ``issued_at``    — timestamptz, NOT NULL.
* ``expires_at``   — timestamptz, NOT NULL. Default 24h from issuance.
* ``consumed_at``  — set when ``verify_email_change`` succeeds.
* ``status``       — 'pending' | 'consumed' | 'expired' | 'cancelled'.
* ``ip``           — request remote address.
* ``user_agent``   — UA string.
* ``tenant_id``    — multi-tenant column, default 'DEFAULT'.

Indexes:

* Partial unique on ``identity_id WHERE status = 'pending'`` — at
  most one in-flight pending row per identity.
* ``(expires_at) WHERE status = 'pending'`` — for the future
  expiry-sweep job.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "portal_auth"
TABLE = "email_change_pending"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "identity_id",
            sa.Text,
            sa.ForeignKey(f"{SCHEMA}.identity.id"),
            nullable=False,
        ),
        sa.Column("new_email", sa.Text, nullable=False),
        sa.Column("code_hash", sa.Text, nullable=False),
        sa.Column("issued_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("consumed_at", TIMESTAMPTZ, nullable=True),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("ip", sa.Text, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_email_change_pending_identity_active",
        TABLE,
        ["identity_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_email_change_pending_expires",
        TABLE,
        ["expires_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_change_pending_expires", table_name=TABLE, schema=SCHEMA
    )
    op.drop_index(
        "uq_email_change_pending_identity_active", table_name=TABLE, schema=SCHEMA
    )
    op.drop_table(TABLE, schema=SCHEMA)
