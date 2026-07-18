"""v2.0: add ``provisioning.processed_event`` inbox-dedup table.

The provisioning-sim `provisioning.task.created` consumer is the one event
consumer that never had inbox dedup (it predates the v1.2 resilient pipeline and
uses a bespoke consume loop rather than the shared ``bind_consumer``). A relay
that re-published the same ``task.created`` event — as happened during the P7 E2E
outbox outage — therefore made the worker re-run the task on every duplicate,
amplifying into a storm. This table lets the consumer claim each ``event_id``
(the relay's AMQP ``message_id``) exactly once, matching the inbox in every other
consuming schema (``order_mgmt`` / ``service_inventory`` / ``subscription``,
created in migration 0027).

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "provisioning"


def upgrade() -> None:
    op.create_table(
        "processed_event",
        sa.Column(
            "event_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("consumer", sa.Text, nullable=False),
        sa.Column(
            "processed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("event_id", "consumer"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("processed_event", schema=SCHEMA)
