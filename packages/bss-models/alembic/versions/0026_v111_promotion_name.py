"""v1.1.1: promotion.name — operator-set friendly label for customer display.

Customers should see "VIP Welcome — 20% off" rather than an internal id or a
bare "20% off". The create-time ``display_name`` (previously only sent to
loyalty's OfferDefinition) is now persisted here so the dashboard + signup
funnel can show it. NULL → display falls back to the discount label.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "catalog"


def upgrade() -> None:
    op.add_column("promotion", sa.Column("name", sa.Text), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("promotion", "name", schema=SCHEMA)
