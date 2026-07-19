"""v1.2: resilient COM/SOM pipeline — outbox relay + inbox dedup + reconciliation.

Three things this migration adds, all additive and reversible:

1. **Outbox relay bookkeeping** on ``audit.domain_event``: ``published_attempts``
   and ``last_publish_error``. The relay (``bss_events.relay``) increments the
   attempt counter and records the last failure so a row that won't publish is
   visible to ``bss trace`` / the cockpit instead of silently stuck. The
   ``ix_domain_event_unpublished`` partial index already exists (migration 0001)
   — it was scaffolded for exactly this relay and was never used until now.

2. **Inbox dedup** — ``<schema>.processed_event`` in each consuming schema
   (``order_mgmt`` for COM, ``service_inventory`` for SOM, ``subscription`` for
   the usage consumer). The safe-consumer helper inserts ``(event_id, consumer)``
   before running a handler; a duplicate redelivery is acked and skipped. Kept
   per-schema (not a shared ``messaging`` schema) to preserve the
   schema-per-service boundary — each service only ever touches its own.

3. **Idempotency + reconciliation columns**:
   - ``subscription.subscription.commercial_order_id`` (+ unique index) so
     ``subscription.create`` is idempotent on the originating order — a redelivered
     ``service_order.completed`` returns the existing subscription instead of
     charging the card-on-file twice.
   - ``order_mgmt.product_order.stuck_flagged_at`` so the reconciliation sweeper
     emits ``order.stuck`` at most once per stranded order.

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INBOX_SCHEMAS = ("order_mgmt", "service_inventory", "subscription")


def _create_inbox(schema: str) -> None:
    op.create_table(
        "processed_event",
        sa.Column("event_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer", sa.Text, nullable=False),
        sa.Column(
            "processed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("event_id", "consumer"),
        schema=schema,
    )


def upgrade() -> None:
    # 1 — outbox relay bookkeeping on the durable event log.
    op.add_column(
        "domain_event",
        sa.Column(
            "published_attempts",
            sa.SmallInteger,
            nullable=False,
            server_default="0",
        ),
        schema="audit",
    )
    op.add_column(
        "domain_event",
        sa.Column("last_publish_error", sa.Text, nullable=True),
        schema="audit",
    )

    # 2 — inbox dedup, one table per consuming schema.
    for schema in _INBOX_SCHEMAS:
        _create_inbox(schema)

    # 3a — subscription idempotency key on the originating commercial order.
    op.add_column(
        "subscription",
        sa.Column("commercial_order_id", sa.Text, nullable=True),
        schema="subscription",
    )
    op.create_index(
        "uq_subscription_commercial_order",
        "subscription",
        ["commercial_order_id"],
        unique=True,
        schema="subscription",
        postgresql_where=sa.text("commercial_order_id IS NOT NULL"),
    )

    # 3b — reconciliation sweeper: at-most-once stuck flag.
    op.add_column(
        "product_order",
        sa.Column("stuck_flagged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="order_mgmt",
    )


def downgrade() -> None:
    op.drop_column("product_order", "stuck_flagged_at", schema="order_mgmt")
    op.drop_index(
        "uq_subscription_commercial_order",
        table_name="subscription",
        schema="subscription",
    )
    op.drop_column("subscription", "commercial_order_id", schema="subscription")
    for schema in _INBOX_SCHEMAS:
        op.drop_table("processed_event", schema=schema)
    op.drop_column("domain_event", "last_publish_error", schema="audit")
    op.drop_column("domain_event", "published_attempts", schema="audit")
