"""v0.18.0: subscription.renewal_reminder_sent_at + reminder-due index.

Backs the v0.18 upcoming-renewal email that the subscription service's
renewal worker sends ~24 hours before ``next_renewal_at``. Same dedup
shape as ``last_renewal_attempted_at``: written by the SELECT-FOR-
UPDATE-SKIP-LOCKED batch the moment the worker decides to send, so a
peer replica or a re-fired tick within the same period boundary skips
the row instantly when the lock releases.

The dedup column is *separate* from ``last_renewal_attempted_at``
because the two events are independent: the reminder fires ~24h
before the renewal, and the renewal itself fires at the period
boundary. Reusing one column for both would require a per-event
window comparison ("did we send the reminder OR did we attempt the
renewal in this period?"), which is harder to reason about than a
column per signal.

Nullable so existing pre-v0.18 rows + the v0.18 backfill subscribe-
already-pending rows are valid without a backfill UPDATE. The first
sweep after deploy fires the reminder for any sub whose
``next_renewal_at`` falls inside the lookahead window, regardless of
the column being NULL — that's the right semantics: a customer
upgrading from v0.17 → v0.18 with a renewal due tomorrow should get
the reminder, not silently skip it because they predate the schema.

Partial index ``ix_subscription_due_for_reminder`` mirrors
``ix_subscription_due_for_renewal`` from migration 0020 — without it
every tick would scan the full subscription table to find rows
whose renewal falls inside the lookahead window.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "subscription"


def upgrade() -> None:
    op.add_column(
        "subscription",
        sa.Column(
            "renewal_reminder_sent_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_subscription_due_for_reminder",
        "subscription",
        ["state", "next_renewal_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscription_due_for_reminder",
        table_name="subscription",
        schema=SCHEMA,
    )
    op.drop_column(
        "subscription", "renewal_reminder_sent_at", schema=SCHEMA
    )
