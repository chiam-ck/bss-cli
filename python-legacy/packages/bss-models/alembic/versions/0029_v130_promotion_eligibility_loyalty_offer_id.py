"""v1.3.0: promotion_eligibility.loyalty_offer_id — upfront customer↔offer pairing.

v1.1.1 retired ``offer.issue`` and consumed everything at activation via
``offer.claim`` by code. v1.3.0 reverses that for the targeted path: the
customer↔offer pairing is minted in loyalty at ``bss promo assign`` time, so
loyalty's per-customer views reflect the assignment immediately (auditability +
operator visibility, the original v1.1.0 model). This column stores the loyalty
offer id; COM uses ``advance_to_claimed`` against it at activation for targeted
promos. Public typed codes still claim-by-code, so their eligibility rows (which
don't exist for public promos anyway) are unaffected.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "catalog"


def upgrade() -> None:
    op.add_column(
        "promotion_eligibility",
        sa.Column("loyalty_offer_id", sa.Text, nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("promotion_eligibility", "loyalty_offer_id", schema=SCHEMA)
