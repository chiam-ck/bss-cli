"""v1.1.1: targeted promos become eligibility-gated codes (not codeless offers).

Phase 0 amendment reversing the v1.1 "targeted = codeless assigned offer"
decision. loyalty's promo_code has no customer field, so a *targeted* promo is
now one real loyalty code + a BSS-side eligibility list: the code auto-applies
for eligible customers and a typed targeted code is rejected for anyone else.

* ``catalog.promotion.audience`` — ``public`` (advertised, anyone may type) or
  ``targeted`` (eligibility-gated, auto-applied). server_default ``public`` so
  the v1.1 rows created before this migration read as public.
* ``catalog.promotion_eligibility`` — (promotion, customer) pairs for targeted
  promos. The per-customer pairing loyalty can't hold lives here; BSS is the gate.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "catalog"


def upgrade() -> None:
    op.add_column(
        "promotion",
        sa.Column(
            "audience", sa.Text, nullable=False, server_default="public"
        ),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_promotion_audience",
        "promotion",
        "audience IN ('public','targeted')",
        schema=SCHEMA,
    )

    op.create_table(
        "promotion_eligibility",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "promotion_id",
            sa.Text,
            sa.ForeignKey(f"{SCHEMA}.promotion.id"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.Text, nullable=False),
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
        schema=SCHEMA,
    )
    # One eligibility row per (promotion, customer, tenant) — idempotent assign.
    op.create_index(
        "uq_promotion_eligibility_promo_customer",
        "promotion_eligibility",
        ["promotion_id", "customer_id", "tenant_id"],
        unique=True,
        schema=SCHEMA,
    )
    # The order-time lookup: "which targeted promos is this customer eligible for?"
    op.create_index(
        "ix_promotion_eligibility_customer",
        "promotion_eligibility",
        ["customer_id", "tenant_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_promotion_eligibility_customer",
        table_name="promotion_eligibility",
        schema=SCHEMA,
    )
    op.drop_index(
        "uq_promotion_eligibility_promo_customer",
        table_name="promotion_eligibility",
        schema=SCHEMA,
    )
    op.drop_table("promotion_eligibility", schema=SCHEMA)
    op.drop_constraint("ck_promotion_audience", "promotion", schema=SCHEMA, type_="check")
    op.drop_column("promotion", "audience", schema=SCHEMA)
