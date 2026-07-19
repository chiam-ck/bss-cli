"""Add Postgres sequences for payment ID generation.

PM-xxx  (payment_method_id_seq)
PAY-xxx (payment_attempt_id_seq)

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-11
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS payment.payment_method_id_seq")
    op.execute("CREATE SEQUENCE IF NOT EXISTS payment.payment_attempt_id_seq")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS payment.payment_attempt_id_seq")
    op.execute("DROP SEQUENCE IF EXISTS payment.payment_method_id_seq")
