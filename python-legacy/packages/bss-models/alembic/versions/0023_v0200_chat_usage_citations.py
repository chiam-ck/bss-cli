"""v0.20.0: audit.chat_usage.citations jsonb.

Adds a per-turn citation record to ``audit.chat_usage`` so when the
cockpit's knowledge tool fires, we can answer "what handbook section
did the operator's last 'how do I rotate the cockpit token' answer
cite?" forensically.

Each cockpit turn (or customer-chat turn, though customer chat does
NOT get the knowledge tool by doctrine) records ``[{anchor,
source_path}, ...]`` for each ``knowledge.search`` / ``knowledge.get``
call resolved in that turn. Replaces nothing — purely additive.

Default ``'[]'::jsonb`` so pre-v0.20 rows + the in-flight rows from
the moment the migration runs to the moment the orchestrator code
ships are valid without a backfill.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "audit"


def upgrade() -> None:
    op.add_column(
        "chat_usage",
        sa.Column(
            "citations",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("chat_usage", "citations", schema=SCHEMA)
