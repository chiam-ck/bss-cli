"""v0.9.0: audit.domain_event.service_identity column.

Adds the ``service_identity`` column to ``audit.domain_event`` so
operators can answer "which surface initiated this write?" via SQL.
The column is populated by each service's ``events/publisher.py`` from
``auth_context.current().service_identity`` (set by RequestIdMiddleware
from the scope key BSSApiTokenMiddleware sets after token validation).

Backfill: existing rows predate the named-token model, so they all
arrived via the v0.3 single-token regime ⇒ ``"default"``. The column
is added NULLABLE, backfilled with ``'default'``, then set NOT NULL
with a server default of ``'default'`` for safety on any straggler
inserts mid-deploy.

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "audit"
TABLE = "domain_event"


def upgrade() -> None:
    # 1. Add NULLABLE column so existing rows survive the schema change.
    op.add_column(
        TABLE,
        sa.Column("service_identity", sa.Text, nullable=True),
        schema=SCHEMA,
    )

    # 2. Backfill historical rows with the v0.3 default identity.
    #    Rationale: all writes prior to v0.9 went through the single
    #    ``BSS_API_TOKEN`` regime, which v0.9 maps to identity "default".
    op.execute(
        f"UPDATE {SCHEMA}.{TABLE} SET service_identity = 'default' "
        "WHERE service_identity IS NULL"
    )

    # 3. Lock down: NOT NULL + server-default for any future inserts
    #    that miss the column at the publisher (paranoia — every
    #    service's publisher.py writes the column explicitly).
    op.alter_column(
        TABLE,
        "service_identity",
        existing_type=sa.Text,
        nullable=False,
        server_default="default",
        schema=SCHEMA,
    )

    # 4. Index for "audit by surface" queries — operators will pivot
    #    on (service_identity, occurred_at) to triage a leaked-token
    #    incident or to compare write volumes per surface.
    op.create_index(
        "ix_domain_event_service_identity_time",
        TABLE,
        ["service_identity", "occurred_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_domain_event_service_identity_time",
        table_name=TABLE,
        schema=SCHEMA,
    )
    op.drop_column(TABLE, "service_identity", schema=SCHEMA)
