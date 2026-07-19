"""v0.12.0: portal_auth.step_up_pending_action — POST body stash for step-up replay.

When ``requires_step_up`` raises ``StepUpRequired`` on a POST, the
customer's typed input is in the form body — and the route handler
hasn't run yet. Stashing the body keyed by ``(session_id,
action_label)`` lets ``verify_step_up`` render an auto-replay page
that POSTs to the original URL with the stashed fields. The customer
types once instead of twice.

Schema:

* ``id``           — text PK (``SUP-...``).
* ``session_id``   — FK to ``portal_auth.session.id``. Stash is
                     scoped to the verified session: a different
                     session cannot replay this row.
* ``action_label`` — same vocabulary as ``login_token.action_label``
                     (``name_update`` / ``vas_purchase`` / etc.).
* ``target_url``   — internal path the replay form posts to. Same
                     ``safe_next_path`` validation as ``next=`` (path
                     and optional query string only — never an
                     absolute URL).
* ``payload_json`` — JSONB. Form fields the customer submitted on
                     the original POST, with ``step_up_token`` stripped.
* ``created_at``, ``expires_at``, ``consumed_at``.

Indexes:

* Partial unique on ``(session_id, action_label) WHERE consumed_at IS NULL``
  — at most one in-flight stash per (session, label). A fresh
  StepUpRequired supersedes the prior unconsumed row.
* ``(expires_at) WHERE consumed_at IS NULL`` — for the future
  expiry-sweep job.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "portal_auth"
TABLE = "step_up_pending_action"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "session_id",
            sa.Text,
            sa.ForeignKey(f"{SCHEMA}.session.id"),
            nullable=False,
        ),
        sa.Column("action_label", sa.Text, nullable=False),
        sa.Column("target_url", sa.Text, nullable=False),
        sa.Column("payload_json", postgresql.JSONB, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("consumed_at", TIMESTAMPTZ, nullable=True),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_step_up_pending_action_active",
        TABLE,
        ["session_id", "action_label"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.create_index(
        "ix_step_up_pending_action_expires",
        TABLE,
        ["expires_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_step_up_pending_action_expires", table_name=TABLE, schema=SCHEMA
    )
    op.drop_index(
        "uq_step_up_pending_action_active", table_name=TABLE, schema=SCHEMA
    )
    op.drop_table(TABLE, schema=SCHEMA)
