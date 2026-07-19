"""Add Postgres sequence for subscription ID generation.

SUB-xxx (subscription_id_seq)

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS subscription.subscription_id_seq")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS subscription.subscription_id_seq")
