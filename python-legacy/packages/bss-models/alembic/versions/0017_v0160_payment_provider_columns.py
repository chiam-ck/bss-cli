"""v0.16.0: payment provider seam — Stripe-ready columns.

Three changes for the v0.16 ``TokenizerAdapter`` seam:

1. New ``payment.customer`` table — per-(BSS customer, provider) cache
   of the provider-side customer reference. ``StripeTokenizerAdapter
   .ensure_customer`` checks this table before calling Stripe so
   subsequent charges skip the round-trip. The CRM customer remains
   the authoritative customer record.

2. New ``payment.payment_method.token_provider`` column — distinguishes
   mock-format ``tok_<uuid>`` rows from Stripe-format ``pm_*`` rows.
   Defaults to ``'mock'`` (back-compat with existing rows). The Track 4
   cutover CLI operates on this column; the lazy-fail path checks it
   before each charge.

3. Two new ``payment.payment_attempt`` columns — ``provider_call_id``
   (Stripe ``pi_*`` for forensic ``bss external-calls`` joins) and
   ``decline_code`` (Stripe's machine-readable decline taxonomy).

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PAYMENT_SCHEMA = "payment"
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "customer",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        sa.Column("customer_external_ref", sa.Text),
        sa.Column("customer_external_ref_provider", sa.Text),
        sa.Column(
            "created_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=PAYMENT_SCHEMA,
    )
    op.create_index(
        "ix_payment_customer_external_ref",
        "customer",
        ["customer_external_ref_provider", "customer_external_ref"],
        schema=PAYMENT_SCHEMA,
    )

    op.add_column(
        "payment_method",
        sa.Column(
            "token_provider",
            sa.Text,
            nullable=False,
            server_default="mock",
        ),
        schema=PAYMENT_SCHEMA,
    )

    op.add_column(
        "payment_attempt",
        sa.Column("provider_call_id", sa.Text),
        schema=PAYMENT_SCHEMA,
    )
    op.add_column(
        "payment_attempt",
        sa.Column("decline_code", sa.Text),
        schema=PAYMENT_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("payment_attempt", "decline_code", schema=PAYMENT_SCHEMA)
    op.drop_column(
        "payment_attempt", "provider_call_id", schema=PAYMENT_SCHEMA
    )
    op.drop_column("payment_method", "token_provider", schema=PAYMENT_SCHEMA)
    op.drop_index(
        "ix_payment_customer_external_ref",
        table_name="customer",
        schema=PAYMENT_SCHEMA,
    )
    op.drop_table("customer", schema=PAYMENT_SCHEMA)
