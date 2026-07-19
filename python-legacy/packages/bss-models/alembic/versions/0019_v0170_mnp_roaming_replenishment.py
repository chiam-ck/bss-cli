"""v0.17.0: MNP (port-in/out) + roaming + MSISDN replenishment.

Three tightly-scoped telco-hygiene gaps closed in one migration:

* ``crm.port_request`` — operator-driven port-in / port-out aggregate
  with its own FSM (``requested → validated → completed | rejected``).
  Distinct from ``crm.case`` so the audit trail and the (post-v0.17)
  TMF629/TMF641 handoff stay clean.
* ``mediation.usage_event.roaming_indicator`` — per-event boolean set
  by the channel/network adapter; rating uses it to route the
  decrement to the ``data_roaming`` BundleBalance instead of ``data``.
  Server default ``false`` is critical: every existing scenario YAML
  posts usage without this field.

The new ``data_roaming`` allowance type and the ``VAS_ROAMING_1GB``
offering ride on the existing open-Text columns
(``bundle_allowance.allowance_type``, ``vas_offering`` rows). They land
via ``bss-seed``, not here. The new ``ported_out`` MSISDN status is an
application-level FSM addition (``inventory.msisdn_pool.status`` is
plain Text by design) — no DB CHECK to add.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CRM_SCHEMA = "crm"
MEDIATION_SCHEMA = "mediation"


def upgrade() -> None:
    op.create_table(
        "port_request",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("donor_carrier", sa.Text, nullable=False),
        sa.Column("donor_msisdn", sa.Text, nullable=False),
        sa.Column("target_subscription_id", sa.Text),
        sa.Column("requested_port_date", sa.Date, nullable=False),
        sa.Column(
            "state",
            sa.Text,
            nullable=False,
            server_default="requested",
        ),
        sa.Column("rejection_reason", sa.Text),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "direction IN ('port_in','port_out')",
            name="ck_port_request_direction",
        ),
        sa.CheckConstraint(
            "state IN ('requested','validated','completed','rejected')",
            name="ck_port_request_state",
        ),
        schema=CRM_SCHEMA,
    )
    # Partial unique index: only one live port at a time per donor_msisdn
    # per tenant. Completed/rejected rows can stack up (audit trail) without
    # blocking a future re-port.
    op.create_index(
        "uq_port_request_donor_pending",
        "port_request",
        ["donor_msisdn", "tenant_id"],
        unique=True,
        schema=CRM_SCHEMA,
        postgresql_where=sa.text("state IN ('requested','validated')"),
    )
    op.create_index(
        "ix_port_request_state_direction",
        "port_request",
        ["state", "direction"],
        schema=CRM_SCHEMA,
    )

    # Backwards-compat critical: server_default 'false' lets pre-v0.17
    # callers (every scenario YAML, every test fixture) post usage
    # without the field and have rows land cleanly.
    op.add_column(
        "usage_event",
        sa.Column(
            "roaming_indicator",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=MEDIATION_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(
        "usage_event", "roaming_indicator", schema=MEDIATION_SCHEMA
    )
    op.drop_index(
        "ix_port_request_state_direction",
        table_name="port_request",
        schema=CRM_SCHEMA,
    )
    op.drop_index(
        "uq_port_request_donor_pending",
        table_name="port_request",
        schema=CRM_SCHEMA,
    )
    op.drop_table("port_request", schema=CRM_SCHEMA)
