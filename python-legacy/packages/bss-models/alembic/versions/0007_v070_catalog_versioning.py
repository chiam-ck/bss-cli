"""v0.7.0: catalog versioning + subscription price snapshot + plan-change pending fields.

Adds time-bound columns to product_offering_price (product_offering already
has them since 0001), price-snapshot columns to subscription (NOT NULL after
in-migration backfill), and three pending-* columns for scheduled plan
changes / operator price migrations.

Also adds three durable price-snapshot columns to order_mgmt.order_item so
COM persists the snapshot at order-creation time (not just on the event bus).

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── catalog.product_offering_price — time-bound rows ────────────────
    op.add_column(
        "product_offering_price",
        sa.Column("valid_from", TIMESTAMPTZ, nullable=True),
        schema="catalog",
    )
    op.add_column(
        "product_offering_price",
        sa.Column("valid_to", TIMESTAMPTZ, nullable=True),
        schema="catalog",
    )

    # ── order_mgmt.order_item — durable price snapshot ──────────────────
    op.add_column(
        "order_item",
        sa.Column("price_amount", sa.Numeric(10, 2), nullable=True),
        schema="order_mgmt",
    )
    op.add_column(
        "order_item",
        sa.Column("price_currency", sa.Text, nullable=True),
        schema="order_mgmt",
    )
    op.add_column(
        "order_item",
        sa.Column("price_offering_price_id", sa.Text, nullable=True),
        schema="order_mgmt",
    )

    # ── subscription.subscription — snapshot + pending fields ───────────
    # Snapshot fields are added NULL-able so backfill can populate them
    # before the NOT NULL constraint flips at the end of the migration.
    op.add_column(
        "subscription",
        sa.Column("price_amount", sa.Numeric(10, 2), nullable=True),
        schema="subscription",
    )
    op.add_column(
        "subscription",
        sa.Column("price_currency", sa.Text, nullable=True),
        schema="subscription",
    )
    op.add_column(
        "subscription",
        sa.Column(
            "price_offering_price_id",
            sa.Text,
            sa.ForeignKey("catalog.product_offering_price.id"),
            nullable=True,
        ),
        schema="subscription",
    )
    op.add_column(
        "subscription",
        sa.Column("pending_offering_id", sa.Text, nullable=True),
        schema="subscription",
    )
    op.add_column(
        "subscription",
        sa.Column("pending_offering_price_id", sa.Text, nullable=True),
        schema="subscription",
    )
    op.add_column(
        "subscription",
        sa.Column("pending_effective_at", TIMESTAMPTZ, nullable=True),
        schema="subscription",
    )

    # ── Backfill snapshot from current catalog price ────────────────────
    # Picks the lowest active recurring price for each subscription's
    # offering — the same contract `get_active_price` enforces from v0.7
    # onwards. ON CONFLICT-style guard not needed; we update where NULL only.
    bind = op.get_bind()
    backfill_sql = sa.text("""
        UPDATE subscription.subscription s
        SET price_amount = p.amount,
            price_currency = p.currency,
            price_offering_price_id = p.id
        FROM (
            SELECT DISTINCT ON (offering_id) id, offering_id, amount, currency
            FROM catalog.product_offering_price
            WHERE price_type = 'recurring'
            ORDER BY offering_id, amount ASC
        ) p
        WHERE p.offering_id = s.offering_id
          AND s.price_amount IS NULL
    """)
    result = bind.execute(backfill_sql)
    # asyncpg/psycopg report rowcount on UPDATE; log it via Alembic's logger.
    print(f"backfilled_subscriptions={result.rowcount}")

    # ── Assert no remaining NULLs before flipping NOT NULL ──────────────
    leftover = bind.execute(
        sa.text(
            "SELECT id, offering_id FROM subscription.subscription "
            "WHERE price_amount IS NULL"
        )
    ).all()
    if leftover:
        ids = [(row[0], row[1]) for row in leftover]
        raise RuntimeError(
            f"backfill incomplete — {len(ids)} subscription(s) had no matching "
            f"recurring price in catalog: {ids}"
        )

    # ── Flip the three snapshot columns to NOT NULL ─────────────────────
    op.alter_column(
        "subscription",
        "price_amount",
        nullable=False,
        schema="subscription",
    )
    op.alter_column(
        "subscription",
        "price_currency",
        nullable=False,
        schema="subscription",
    )
    op.alter_column(
        "subscription",
        "price_offering_price_id",
        nullable=False,
        schema="subscription",
    )


def downgrade() -> None:
    op.drop_column("subscription", "pending_effective_at", schema="subscription")
    op.drop_column("subscription", "pending_offering_price_id", schema="subscription")
    op.drop_column("subscription", "pending_offering_id", schema="subscription")
    op.drop_column("subscription", "price_offering_price_id", schema="subscription")
    op.drop_column("subscription", "price_currency", schema="subscription")
    op.drop_column("subscription", "price_amount", schema="subscription")

    op.drop_column("order_item", "price_offering_price_id", schema="order_mgmt")
    op.drop_column("order_item", "price_currency", schema="order_mgmt")
    op.drop_column("order_item", "price_amount", schema="order_mgmt")

    op.drop_column("product_offering_price", "valid_to", schema="catalog")
    op.drop_column("product_offering_price", "valid_from", schema="catalog")
