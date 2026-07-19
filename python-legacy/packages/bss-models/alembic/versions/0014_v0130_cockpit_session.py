"""v0.13.0: cockpit schema (3 tables) for the operator-cockpit Conversation store.

Adds the operator-side Conversation store backing both surfaces of the
unified cockpit (the canonical CLI REPL + the browser veneer). The
schema is intentionally separate from ``crm`` and ``portal_auth`` —
this is operator-side state, not customer-side.

Three tables:

* ``cockpit.session`` — one row per cockpit session. ``actor`` is the
  operator name from ``.bss-cli/settings.toml``; ``customer_focus`` is
  a pinned ``CUST-NNN`` carried into the system prompt; ``state`` is
  ``'active' | 'closed'``; ``allow_destructive`` defaults false but is
  toggled per-turn by ``/confirm``.
* ``cockpit.message`` — append-only conversation log. ``role`` is
  ``'user' | 'assistant' | 'tool'``; ``tool_calls_json`` carries the
  v0.12 AgentEvent shape for the assistant turn that proposed them.
* ``cockpit.pending_destructive`` — at-most-one in-flight propose row
  per session. The agent proposes; ``/confirm`` consumes the row and
  flips the next turn to ``allow_destructive=True``.

All three tables carry a ``tenant_id`` column per doctrine (single
DEFAULT for v0.13; the column is the seam for a later split).

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "cockpit"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    # ── session ──────────────────────────────────────────────────────
    op.create_table(
        "session",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("customer_focus", sa.Text, nullable=True),
        sa.Column(
            "allow_destructive",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "state",
            sa.Text,
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "started_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_active_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("label", sa.Text, nullable=True),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    # /sessions for an operator: list active sessions newest-first.
    op.create_index(
        "ix_cockpit_session_actor_active",
        "session",
        ["actor", "last_active_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("state = 'active'"),
    )

    # ── message ──────────────────────────────────────────────────────
    op.create_table(
        "message",
        sa.Column(
            "id", sa.BigInteger, primary_key=True, autoincrement=True
        ),
        sa.Column(
            "session_id",
            sa.Text,
            sa.ForeignKey(f"{SCHEMA}.session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 'user' | 'assistant' | 'tool'
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("tool_calls_json", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    # transcript_text() reads in created_at order per session.
    op.create_index(
        "ix_cockpit_message_session_created",
        "message",
        ["session_id", "created_at"],
        schema=SCHEMA,
    )

    # ── pending_destructive ─────────────────────────────────────────
    op.create_table(
        "pending_destructive",
        sa.Column(
            "session_id",
            sa.Text,
            sa.ForeignKey(f"{SCHEMA}.session.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "proposed_at",
            TIMESTAMPTZ,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("tool_args_json", sa.JSON, nullable=False),
        sa.Column(
            "proposal_message_id",
            sa.BigInteger,
            sa.ForeignKey(f"{SCHEMA}.message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("pending_destructive", schema=SCHEMA)

    op.drop_index(
        "ix_cockpit_message_session_created",
        table_name="message",
        schema=SCHEMA,
    )
    op.drop_table("message", schema=SCHEMA)

    op.drop_index(
        "ix_cockpit_session_actor_active",
        table_name="session",
        schema=SCHEMA,
    )
    op.drop_table("session", schema=SCHEMA)

    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
