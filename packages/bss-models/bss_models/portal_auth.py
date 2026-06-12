"""Portal auth schema — 4 tables.

identity, login_token, session, login_attempt.

This schema is portal-side identity, deliberately separate from
``crm`` (which owns the BSS-core customer record). An ``identity``
becomes linked to a ``customer`` only when the signup funnel
completes its first KYC + ``customer.create`` (see
``link_to_customer`` in the bss-portal-auth public API). Until then,
``customer_id`` is NULL and the visitor sees the empty dashboard.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, TZDateTime

SCHEMA = "portal_auth"


class Identity(Base, TenantMixin):
    """A verified email maps to one identity, optionally linked to a customer."""

    __tablename__ = "identity"
    __table_args__ = (
        # Same email can't have two active identities; only `deleted` is excluded
        # from the uniqueness check, so soft-deleted rows don't block re-signup.
        Index(
            "uq_identity_email_active",
            "email",
            unique=True,
            postgresql_where=text("status <> 'deleted'"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str | None] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="unverified", server_default="unverified"
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class LoginToken(Base, TenantMixin):
    """Active login challenge — OTP, magic_link, or step_up."""

    __tablename__ = "login_token"
    __table_args__ = (
        Index(
            "ix_login_token_identity_kind_unconsumed",
            "identity_id",
            "kind",
            "consumed_at",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    identity_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.identity.id"), nullable=False
    )
    # 'magic_link' | 'otp' | 'step_up'
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional scope binding for step_up tokens (e.g. "subscription.terminate").
    # Null for magic_link / otp; never null for step_up.
    action_label: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)


class Session(Base, TenantMixin):
    """Server-side session — the cookie value is just this row's id."""

    __tablename__ = "session"
    __table_args__ = (
        Index("ix_session_identity_active", "identity_id", "revoked_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    identity_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.identity.id"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class LoginAttempt(Base, TenantMixin):
    """Append-only audit + rate-limit substrate.

    `outcome` ∈ {success, wrong_code, expired, no_active_token,
    rate_limited, no_such_identity}. Entries are queried by
    (email, ip, ts) windows to enforce per-email and per-IP caps.
    """

    __tablename__ = "login_attempt"
    __table_args__ = (
        Index("ix_login_attempt_email_ts", "email", "ts"),
        Index("ix_login_attempt_ip_ts", "ip", "ts"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    # Optional discriminator: 'login_start', 'login_verify', 'step_up_start',
    # 'step_up_verify'. Helps the rate-limiter scope its window queries.
    stage: Mapped[str | None] = mapped_column(Text)


class EmailChangePending(Base, TenantMixin):
    """v0.10 — pending email-change verification.

    The customer entered a new email and received an OTP at it. Until
    they verify or the row expires (24h), the OTP can be redeemed to
    atomically swap the email on both ``crm.contact_medium`` and
    ``portal_auth.identity.email``. Re-starting the flow voids the
    prior pending row.
    """

    __tablename__ = "email_change_pending"
    __table_args__ = (
        Index(
            "uq_email_change_pending_identity_active",
            "identity_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_email_change_pending_expires",
            "expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    identity_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.identity.id"), nullable=False
    )
    new_email: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)


class StepUpPendingAction(Base, TenantMixin):
    """v0.12 — pending POST body stashed on StepUpRequired.

    When ``requires_step_up`` raises on a POST, the route handler hasn't
    run yet — but the customer's typed input is in the form body. We
    stash that body keyed by ``(session_id, action_label)`` so that
    ``verify_step_up`` can render an auto-replay page (POST to the
    original URL with the stashed fields) and the customer doesn't
    re-type. One unconsumed row per (session, action_label); a fresh
    StepUpRequired supersedes any prior unconsumed row.
    """

    __tablename__ = "step_up_pending_action"
    __table_args__ = (
        Index(
            "uq_step_up_pending_action_active",
            "session_id",
            "action_label",
            unique=True,
            postgresql_where=text("consumed_at IS NULL"),
        ),
        Index(
            "ix_step_up_pending_action_expires",
            "expires_at",
            postgresql_where=text("consumed_at IS NULL"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.session.id"), nullable=False
    )
    action_label: Mapped[str] = mapped_column(Text, nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class PortalAction(Base, TenantMixin):
    """v0.10 — per-write portal-side audit row.

    Written by every direct post-login self-serve route after the BSS
    write completes (success or failure). Complements the canonical
    ``audit.domain_event`` row with portal-side context: which route,
    which resolved customer principal, was step-up consumed, and the
    originating ip / user agent. The forensic question this answers
    is "did customer X actually authorise this?" — not the BSS
    domain question of "what changed in the canonical record".
    """

    __tablename__ = "portal_action"
    __table_args__ = (
        Index(
            "ix_portal_action_customer_ts",
            "customer_id",
            text("ts DESC"),
        ),
        Index(
            "ix_portal_action_action_ts",
            "action",
            text("ts DESC"),
        ),
        Index(
            "ix_portal_action_unknown_rule",
            "error_rule",
            text("ts DESC"),
            postgresql_where=text("error_rule IS NOT NULL"),
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    customer_id: Mapped[str | None] = mapped_column(Text)
    identity_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_rule: Mapped[str | None] = mapped_column(Text)
    step_up_consumed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
