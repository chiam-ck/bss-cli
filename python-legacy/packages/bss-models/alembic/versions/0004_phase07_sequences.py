"""Add Postgres sequences for Phase 7: COM, SOM, provisioning-sim.

ORD-xxx, OI-xxx, SO-xxx, SOI-xxx, SVC-xxx, PTK-xxx

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # COM — order management
    op.execute("CREATE SEQUENCE IF NOT EXISTS order_mgmt.product_order_id_seq")
    op.execute("CREATE SEQUENCE IF NOT EXISTS order_mgmt.order_item_id_seq")

    # SOM — service inventory
    op.execute("CREATE SEQUENCE IF NOT EXISTS service_inventory.service_order_id_seq")
    op.execute("CREATE SEQUENCE IF NOT EXISTS service_inventory.service_order_item_id_seq")
    op.execute("CREATE SEQUENCE IF NOT EXISTS service_inventory.service_id_seq")

    # Provisioning simulator
    op.execute("CREATE SEQUENCE IF NOT EXISTS provisioning.task_id_seq")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS provisioning.task_id_seq")
    op.execute("DROP SEQUENCE IF EXISTS service_inventory.service_id_seq")
    op.execute("DROP SEQUENCE IF EXISTS service_inventory.service_order_item_id_seq")
    op.execute("DROP SEQUENCE IF EXISTS service_inventory.service_order_id_seq")
    op.execute("DROP SEQUENCE IF EXISTS order_mgmt.order_item_id_seq")
    op.execute("DROP SEQUENCE IF EXISTS order_mgmt.product_order_id_seq")
