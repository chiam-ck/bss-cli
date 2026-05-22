"""v1.1.0: promotions via loyalty-cli integration.

Three schema deltas in one migration family — the entire BSS-side data
footprint of v1.1. loyalty-cli holds the entitlement ledger; BSS holds
only the money terms + the per-subscription discount counter.

* ``catalog.promotion`` — the one genuinely new domain object. Money
  terms (discount type/value, applicable offerings, duration) plus the
  loyalty join key ``offer_definition_id``. NULL OD + ``state='pending_link'``
  while the create saga is mid-flight; ``state='active'`` once loyalty's
  ``offer_definition.register`` returns. No FK to loyalty (HTTP boundary).
  ``code`` is NULL for codeless *targeted* promotions (assigned via
  ``offer.issue``, never typed); a partial unique index enforces one
  promotion per non-null code per tenant.

* ``subscription.subscription`` — discount snapshot columns. ``price_amount``
  stays the FULL base; effective is computed at charge time. Mirrors the
  v0.7 ``pending_*`` plan-change pattern. ``discount_periods_remaining``
  defaults to 0 (server_default critical: every existing subscription row
  predates v1.1 and must read as "no discount"). ``-1`` = perpetual.

* ``order_mgmt.order_item`` — discount intent stamped at order create,
  carried into the subscription snapshot at activation. ``promo_offer_id``
  is the loyalty offer captured at claim, used for redeem/revoke.

CHECK constraints on the closed value sets (``discount_type``,
``duration_kind``, ``state``) follow the v0.17 ``port_request`` precedent:
the FSM logic lives in app code; the DB check is a backstop against an
impossible value, not the state machine itself.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CATALOG_SCHEMA = "catalog"
SUBSCRIPTION_SCHEMA = "subscription"
ORDER_SCHEMA = "order_mgmt"


def upgrade() -> None:
    # ── catalog.promotion ───────────────────────────────────────────────
    op.create_table(
        "promotion",
        sa.Column("id", sa.Text, primary_key=True),  # e.g. PROMO_SUMMER25
        sa.Column("code", sa.Text),  # NULL for codeless targeted promos
        sa.Column("offer_definition_id", sa.Text),  # loyalty join key; NULL until saga done
        sa.Column("discount_type", sa.Text, nullable=False),  # percent | absolute
        sa.Column("discount_value", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.Text, nullable=False, server_default="SGD"),
        sa.Column(
            "applicable_offering_ids",
            sa.dialects.postgresql.ARRAY(sa.Text),
        ),  # NULL = all sellable offerings
        sa.Column("duration_kind", sa.Text, nullable=False),  # single | multi | perpetual
        sa.Column("periods_total", sa.SmallInteger),  # N for multi; NULL otherwise
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True)),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True)),
        sa.Column("state", sa.Text, nullable=False),  # pending_link → active → retired
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("tenant_id", sa.Text, nullable=False, server_default="DEFAULT"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "discount_type IN ('percent','absolute')",
            name="ck_promotion_discount_type",
        ),
        sa.CheckConstraint(
            "duration_kind IN ('single','multi','perpetual')",
            name="ck_promotion_duration_kind",
        ),
        sa.CheckConstraint(
            "state IN ('pending_link','active','retired')",
            name="ck_promotion_state",
        ),
        # A multi-period promo must carry its period count; single/perpetual
        # must not. Keeps the renewal-counter math unambiguous.
        sa.CheckConstraint(
            "(duration_kind = 'multi') = (periods_total IS NOT NULL)",
            name="ck_promotion_periods_total_matches_kind",
        ),
        schema=CATALOG_SCHEMA,
    )
    # get_by_offer_definition_id (validation/preview) + promo reconcile relink.
    op.create_index(
        "ix_promotion_offer_definition_id",
        "promotion",
        ["offer_definition_id"],
        schema=CATALOG_SCHEMA,
    )
    # One promotion per non-null code per tenant. Codeless (NULL) targeted
    # promos stack freely. Partial so NULLs don't collide.
    op.create_index(
        "uq_promotion_code",
        "promotion",
        ["code", "tenant_id"],
        unique=True,
        schema=CATALOG_SCHEMA,
        postgresql_where=sa.text("code IS NOT NULL"),
    )

    # ── subscription.subscription — discount snapshot ───────────────────
    op.add_column(
        "subscription",
        sa.Column("discount_type", sa.Text),
        schema=SUBSCRIPTION_SCHEMA,
    )
    op.add_column(
        "subscription",
        sa.Column("discount_value", sa.Numeric(12, 2)),
        schema=SUBSCRIPTION_SCHEMA,
    )
    # server_default 0 critical: pre-v1.1 rows must read as "no discount".
    op.add_column(
        "subscription",
        sa.Column(
            "discount_periods_remaining",
            sa.SmallInteger,
            nullable=False,
            server_default="0",
        ),
        schema=SUBSCRIPTION_SCHEMA,
    )
    op.add_column(
        "subscription",
        sa.Column("promo_code", sa.Text),
        schema=SUBSCRIPTION_SCHEMA,
    )
    op.add_column(
        "subscription",
        sa.Column("promo_offer_definition_id", sa.Text),
        schema=SUBSCRIPTION_SCHEMA,
    )

    # ── order_mgmt.order_item — discount intent ─────────────────────────
    op.add_column("order_item", sa.Column("discount_code", sa.Text), schema=ORDER_SCHEMA)
    op.add_column(
        "order_item", sa.Column("promo_offer_definition_id", sa.Text), schema=ORDER_SCHEMA
    )
    op.add_column("order_item", sa.Column("discount_type", sa.Text), schema=ORDER_SCHEMA)
    op.add_column(
        "order_item", sa.Column("discount_value", sa.Numeric(12, 2)), schema=ORDER_SCHEMA
    )
    op.add_column(
        "order_item", sa.Column("discount_periods_total", sa.SmallInteger), schema=ORDER_SCHEMA
    )
    op.add_column("order_item", sa.Column("promo_offer_id", sa.Text), schema=ORDER_SCHEMA)


def downgrade() -> None:
    op.drop_column("order_item", "promo_offer_id", schema=ORDER_SCHEMA)
    op.drop_column("order_item", "discount_periods_total", schema=ORDER_SCHEMA)
    op.drop_column("order_item", "discount_value", schema=ORDER_SCHEMA)
    op.drop_column("order_item", "discount_type", schema=ORDER_SCHEMA)
    op.drop_column("order_item", "promo_offer_definition_id", schema=ORDER_SCHEMA)
    op.drop_column("order_item", "discount_code", schema=ORDER_SCHEMA)

    op.drop_column("subscription", "promo_offer_definition_id", schema=SUBSCRIPTION_SCHEMA)
    op.drop_column("subscription", "promo_code", schema=SUBSCRIPTION_SCHEMA)
    op.drop_column("subscription", "discount_periods_remaining", schema=SUBSCRIPTION_SCHEMA)
    op.drop_column("subscription", "discount_value", schema=SUBSCRIPTION_SCHEMA)
    op.drop_column("subscription", "discount_type", schema=SUBSCRIPTION_SCHEMA)

    op.drop_index("uq_promotion_code", table_name="promotion", schema=CATALOG_SCHEMA)
    op.drop_index(
        "ix_promotion_offer_definition_id", table_name="promotion", schema=CATALOG_SCHEMA
    )
    op.drop_table("promotion", schema=CATALOG_SCHEMA)
