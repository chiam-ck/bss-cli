"""v0.18.0: subscription.last_renewal_attempted_at + due-renewal index.

Backs the v0.18 in-process renewal worker (`services/subscription/app/
workers/renewal.py`). The column is the per-period idempotency dedup —
the worker writes it BEFORE dispatching `renew()` inside the same
SELECT-FOR-UPDATE-SKIP-LOCKED transaction so a peer replica or a
re-fired tick on the same row picks up the freshly-marked dedup state
the moment the lock releases. Same column dedups both sweeps (active-
due + blocked-overdue) — single signal per (sub, period_boundary).

Nullable so existing v0.17 rows are valid without backfill (server-
default would defeat the dedup invariant since every prior row would
look already-attempted at migration time). Pre-v0.18 subscriptions
become immediately due once `next_renewal_at <= clock_now()` regardless
of the column being NULL — that's the correct semantics for a soak
machine: the first sweep after deploy renews everything that's overdue.

Partial index `ix_subscription_due_for_renewal` backs both sweep
queries; without it the worker scans the whole subscription table on
every tick interval.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "subscription"


def upgrade() -> None:
    op.add_column(
        "subscription",
        sa.Column(
            "last_renewal_attempted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_subscription_due_for_renewal",
        "subscription",
        ["state", "next_renewal_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("state IN ('active','blocked')"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscription_due_for_renewal",
        table_name="subscription",
        schema=SCHEMA,
    )
    op.drop_column(
        "subscription", "last_renewal_attempted_at", schema=SCHEMA
    )
