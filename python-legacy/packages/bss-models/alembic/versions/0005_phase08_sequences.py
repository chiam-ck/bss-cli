"""Add Postgres sequences for Phase 8: mediation.

UE-xxxxxx (mediation.usage_event_id_seq)

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS mediation.usage_event_id_seq")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS mediation.usage_event_id_seq")
