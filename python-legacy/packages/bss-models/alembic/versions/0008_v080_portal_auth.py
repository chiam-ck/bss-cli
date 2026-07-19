"""v0.8.0: portal_auth schema (4 tables) for self-serve portal identity.

Adds the portal-side identity layer: an email-based identity, login
tokens (magic link / OTP / step-up), server-side sessions, and an
append-only login-attempt audit / rate-limit log.

This schema is portal-side, intentionally separate from ``crm``.
``identity.customer_id`` is NULL until the signup funnel calls
``bss_portal_auth.link_to_customer`` — see phases/V0_8_0.md §3.3.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

TIMESTAMPTZ = sa.DateTime(timezone=True)

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "portal_auth"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    # ── identity ─────────────────────────────────────────────────────
    op.create_table(
        "identity",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("customer_id", sa.Text, nullable=True),
        sa.Column("email_verified_at", TIMESTAMPTZ, nullable=True),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default="unverified",
        ),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_login_at", TIMESTAMPTZ, nullable=True),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    # Same email can't have two active identities; soft-deleted rows
    # (status='deleted') are excluded so re-signup after deletion is allowed.
    op.create_index(
        "uq_identity_email_active",
        "identity",
        ["email"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status <> 'deleted'"),
    )

    # ── login_token ──────────────────────────────────────────────────
    op.create_table(
        "login_token",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "identity_id",
            sa.Text,
            sa.ForeignKey(f"{SCHEMA}.identity.id"),
            nullable=False,
        ),
        # 'magic_link' | 'otp' | 'step_up'
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("code_hash", sa.Text, nullable=False),
        # Required for kind='step_up'; null for magic_link / otp.
        sa.Column("action_label", sa.Text, nullable=True),
        sa.Column("issued_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("consumed_at", TIMESTAMPTZ, nullable=True),
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
        "ix_login_token_identity_kind_unconsumed",
        "login_token",
        ["identity_id", "kind", "consumed_at"],
        schema=SCHEMA,
    )

    # ── session ──────────────────────────────────────────────────────
    op.create_table(
        "session",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "identity_id",
            sa.Text,
            sa.ForeignKey(f"{SCHEMA}.identity.id"),
            nullable=False,
        ),
        sa.Column("issued_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_seen_at", TIMESTAMPTZ, nullable=False),
        sa.Column("ip", sa.Text, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("revoked_at", TIMESTAMPTZ, nullable=True),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_session_identity_active",
        "session",
        ["identity_id", "revoked_at"],
        schema=SCHEMA,
    )

    # ── login_attempt ────────────────────────────────────────────────
    op.create_table(
        "login_attempt",
        sa.Column(
            "id",
            sa.BigInteger,
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("email", sa.Text, nullable=True),
        sa.Column("ip", sa.Text, nullable=True),
        sa.Column("ts", TIMESTAMPTZ, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        # 'login_start' | 'login_verify' | 'step_up_start' | 'step_up_verify'
        sa.Column("stage", sa.Text, nullable=True),
        sa.Column(
            "tenant_id",
            sa.Text,
            nullable=False,
            server_default="DEFAULT",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_login_attempt_email_ts",
        "login_attempt",
        ["email", "ts"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_login_attempt_ip_ts",
        "login_attempt",
        ["ip", "ts"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_login_attempt_ip_ts", table_name="login_attempt", schema=SCHEMA
    )
    op.drop_index(
        "ix_login_attempt_email_ts", table_name="login_attempt", schema=SCHEMA
    )
    op.drop_table("login_attempt", schema=SCHEMA)

    op.drop_index(
        "ix_session_identity_active", table_name="session", schema=SCHEMA
    )
    op.drop_table("session", schema=SCHEMA)

    op.drop_index(
        "ix_login_token_identity_kind_unconsumed",
        table_name="login_token",
        schema=SCHEMA,
    )
    op.drop_table("login_token", schema=SCHEMA)

    op.drop_index(
        "uq_identity_email_active", table_name="identity", schema=SCHEMA
    )
    op.drop_table("identity", schema=SCHEMA)

    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
