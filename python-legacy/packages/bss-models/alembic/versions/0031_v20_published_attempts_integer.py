"""v2.0: widen ``audit.domain_event.published_attempts`` SmallInteger → Integer.

The outbox relay increments ``published_attempts`` on every delivery attempt
(``MARK_OK_SQL`` / ``MARK_FAIL_SQL`` in ``bss_events.relay``). SmallInteger caps
at 32767. When the Rust ``MqChannel`` failed to recover from a dropped AMQP
channel (a transient broker blip), the relay retried a single unpublished row
several times a second for days, and ``published_attempts + 1`` eventually raised
``smallint out of range`` — which aborts the whole ``drain_once`` transaction and
wedges the relay tick, so *no* row publishes. Widening to Integer removes that
overflow failure mode (the underlying no-reconnect defect is fixed separately in
``bss_events::mq``).

Non-destructive: SmallInteger → Integer is an in-place widening, no data loss.

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "audit"


def upgrade() -> None:
    op.alter_column(
        "domain_event",
        "published_attempts",
        schema=SCHEMA,
        type_=sa.Integer(),
        existing_type=sa.SmallInteger(),
        existing_nullable=False,
        existing_server_default="0",
    )


def downgrade() -> None:
    # Reversible, but a downgrade re-introduces the 32767 ceiling. Values above it
    # (from a storming relay) would fail the cast — acceptable for a downgrade path.
    op.alter_column(
        "domain_event",
        "published_attempts",
        schema=SCHEMA,
        type_=sa.SmallInteger(),
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default="0",
    )
