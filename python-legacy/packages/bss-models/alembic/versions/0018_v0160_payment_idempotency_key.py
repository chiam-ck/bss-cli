"""v0.16.0: payment_attempt.idempotency_key column.

Persists the v0.16 ``ATT-{id}-r{retry_count}`` key on every charge attempt
row. v0.16 always uses ``r0`` (one attempt = one row = one key) — the
crash-restart-detect path that reuses the key on a retry of the same
attempt is a v1.0 concern documented in ``docs/runbooks/payment-idempotency.md``.

The column is nullable so existing pre-v0.16 rows remain valid without
backfill; v0.16+ writes always populate.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PAYMENT_SCHEMA = "payment"


def upgrade() -> None:
    op.add_column(
        "payment_attempt",
        sa.Column("idempotency_key", sa.Text),
        schema=PAYMENT_SCHEMA,
    )
    # Forensic index — `bss external-calls --idempotency-key X` joins
    # against this. Sparse and append-only, so a partial index keeps
    # the cost low.
    op.create_index(
        "ix_payment_attempt_idempotency_key",
        "payment_attempt",
        ["idempotency_key"],
        schema=PAYMENT_SCHEMA,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_payment_attempt_idempotency_key",
        table_name="payment_attempt",
        schema=PAYMENT_SCHEMA,
    )
    op.drop_column(
        "payment_attempt", "idempotency_key", schema=PAYMENT_SCHEMA
    )
