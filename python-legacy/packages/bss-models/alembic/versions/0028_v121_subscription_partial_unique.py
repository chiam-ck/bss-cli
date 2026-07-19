"""v1.2.1: subscription msisdn/iccid uniqueness ignores terminated rows.

Before: ``UNIQUE (msisdn)`` and ``UNIQUE (iccid)`` constraints on
``subscription.subscription`` treated terminated subscriptions as still
owning their numbers, so when inventory legitimately recycled a phone
number / eSIM profile back to ``available`` a new customer's signup
would brick at ``subscription.create`` with
``IntegrityError: duplicate key value violates unique constraint
"uq_subscription_msisdn"``. Service order completed, eSIM provisioned,
order zombied in_progress with no subscription.

Fix: drop the broad UNIQUE constraints and replace with partial unique
indices that exclude terminated rows. Two active subscriptions still
cannot share a number (the meaningful invariant); terminated rows keep
their old numbers for audit but stop blocking inventory reuse.

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "subscription"
TABLE = "subscription"
_WHERE_ACTIVE = "state <> 'terminated'"


def upgrade() -> None:
    op.drop_constraint(
        "uq_subscription_msisdn", TABLE, schema=SCHEMA, type_="unique"
    )
    op.drop_constraint(
        "uq_subscription_iccid", TABLE, schema=SCHEMA, type_="unique"
    )
    op.create_index(
        "uq_subscription_msisdn",
        TABLE,
        ["msisdn"],
        schema=SCHEMA,
        unique=True,
        postgresql_where=sa.text(_WHERE_ACTIVE),
    )
    op.create_index(
        "uq_subscription_iccid",
        TABLE,
        ["iccid"],
        schema=SCHEMA,
        unique=True,
        postgresql_where=sa.text(_WHERE_ACTIVE),
    )


def downgrade() -> None:
    op.drop_index("uq_subscription_msisdn", table_name=TABLE, schema=SCHEMA)
    op.drop_index("uq_subscription_iccid", table_name=TABLE, schema=SCHEMA)
    op.create_unique_constraint(
        "uq_subscription_msisdn", TABLE, ["msisdn"], schema=SCHEMA
    )
    op.create_unique_constraint(
        "uq_subscription_iccid", TABLE, ["iccid"], schema=SCHEMA
    )
