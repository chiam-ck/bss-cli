"""v0.10.0: portal_auth.portal_action — per-write portal-side audit table.

Every direct post-login write from the self-serve portal records a
row here so ops can answer "did customer X actually authorise this?"
without joining across BSS service audit logs. This complements the
service-side ``audit.domain_event`` row (which captures the canonical
domain change) with portal-side context: the route, the resolved
customer principal, whether step-up was consumed, and the originating
IP / user agent.

Schema (portal_auth.portal_action):

* ``id``               — bigint, autoincrement.
* ``ts``               — write timestamp (timestamptz, NOT NULL).
* ``customer_id``      — resolved from ``request.state.customer_id``.
* ``identity_id``      — the verified portal identity that initiated.
* ``action``           — the SENSITIVE_ACTION_LABELS member, plus a
                         small set of read-mode labels for forensics
                         (e.g. ``esim_view_full``).
* ``route``            — the request path (e.g. ``/top-up``).
* ``method``           — HTTP method (POST / GET — GET only when the
                         route is sensitive enough to log a read,
                         like ``esim_view_full``).
* ``success``          — boolean: did the BSS write succeed?
* ``error_rule``       — the PolicyViolation rule when ``success=false``,
                         else NULL. Unknown-to-i18n rules surface here
                         so ops can register customer-facing copy.
* ``step_up_consumed`` — boolean: was a step-up token consumed during
                         this request? Distinct from "step-up required";
                         a route can be sensitive without consuming a
                         token on this exact attempt (see GET /esim).
* ``ip``               — request remote address.
* ``user_agent``       — UA string.
* ``tenant_id``        — multi-tenant column (DEFAULT seeded), per
                         project doctrine.

Indexes:

* ``(customer_id, ts DESC)`` — "show me everything this customer did
  via the portal in the last 30 days" — the primary forensic query.
* ``(action, ts DESC)``      — "which routes are failing this week?"
* ``(error_rule, ts DESC) WHERE error_rule IS NOT NULL`` — partial
  index for the "unknown rule -> register copy" backlog.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "portal_auth"
TABLE = "portal_action"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", TIMESTAMPTZ, nullable=False),
        sa.Column("customer_id", sa.Text, nullable=True),
        sa.Column("identity_id", sa.Text, nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("route", sa.Text, nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("error_rule", sa.Text, nullable=True),
        sa.Column(
            "step_up_consumed",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("ip", sa.Text, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_portal_action_customer_ts",
        TABLE,
        ["customer_id", sa.text("ts DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_portal_action_action_ts",
        TABLE,
        ["action", sa.text("ts DESC")],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_portal_action_unknown_rule",
        TABLE,
        ["error_rule", sa.text("ts DESC")],
        schema=SCHEMA,
        postgresql_where=sa.text("error_rule IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portal_action_unknown_rule", table_name=TABLE, schema=SCHEMA
    )
    op.drop_index(
        "ix_portal_action_action_ts", table_name=TABLE, schema=SCHEMA
    )
    op.drop_index(
        "ix_portal_action_customer_ts", table_name=TABLE, schema=SCHEMA
    )
    op.drop_table(TABLE, schema=SCHEMA)
