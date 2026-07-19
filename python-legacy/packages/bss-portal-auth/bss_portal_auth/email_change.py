"""Email-change two-step flow with cross-schema atomic verification (v0.10 PR 8).

V0_10_0.md "Do not consider the email-change flow done until the
rollback test passes" — the CRM contact-medium update and the
``portal_auth.identity.email`` update must commit together or roll
back together. Sequential commits leave the system in a mismatched
state (CRM has new email, portal_auth has old one) with no easy
recovery; that is the explicit anti-pattern v0.10 forbids.

This module is the single, named place where the cross-schema write
lives. Both schemas (``crm`` and ``portal_auth``) live in the same
Postgres instance, so a single ``AsyncSession`` transaction spans
them. ``bss_portal_auth`` is the right home for the function: it
already owns the portal-side identity layer + the DB session, and
``bss_models`` exports both schemas' ORM classes.

Doctrine note: writing directly to ``crm.contact_medium`` from a
non-CRM module is a deliberate, narrow exception to "writes go
through service-side policies." The exception is documented in
DECISIONS 2026-04-27 (the "v0.10 PR 8" entry). Future flows that
need similar atomicity must either (a) use this same exception with
a documented justification, (b) add a service-side composite
operation, or (c) pick a saga pattern with explicit compensation.
The default is still "go through the service HTTP API."

Public surface:

* ``start_email_change(db, identity_id, new_email, ip, user_agent,
  email_adapter)`` — uniqueness check, mint OTP, persist pending row,
  send OTP to the *new* email. Caller commits.
* ``verify_email_change(db, identity_id, code)`` — match OTP against
  pending row, atomically: update CRM contact_medium for email +
  update portal_auth.identity.email + mark pending consumed. Caller
  commits.
* ``cancel_pending_email_change(db, identity_id)`` — explicit cancel.
* ``EmailChangeStarted`` / ``EmailChangeFailed`` / ``EmailChangeApplied`` —
  result types so the route handler can branch without parsing
  exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from bss_clock import now as clock_now
from bss_models import (
    ContactMedium,
    Customer,
    EmailChangePending,
    Identity,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .email import EmailAdapter
from .service import _login_token_id
from .tokens import generate_otp, hash_token, verify_token

# ── Result types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmailChangeStarted:
    """A pending row exists; the OTP is in transit to ``new_email``."""

    pending_id: str
    new_email: str


@dataclass(frozen=True)
class EmailChangeApplied:
    """Cross-schema commit completed; both rows now reflect the new email."""

    new_email: str


@dataclass(frozen=True)
class EmailChangeFailed:
    """Generic failure with a structured reason for the route to branch on."""

    reason: str  # 'no_active_pending' | 'wrong_code' | 'expired' | 'email_in_use'


# ── start_email_change ───────────────────────────────────────────────────


async def start_email_change(
    db: AsyncSession,
    *,
    identity_id: str,
    new_email: str,
    ip: str | None,
    user_agent: str | None,
    email_adapter: EmailAdapter,
) -> EmailChangeStarted | EmailChangeFailed:
    """Begin an email-change flow.

    Steps (all in the caller's transaction):

    1. Reject if ``new_email`` is already in active use as an email
       contact medium on any customer (uniqueness check up-front so
       the customer hears about it BEFORE waiting for an OTP).
    2. Cancel any prior pending row for this identity (re-starting
       the flow voids the old OTP).
    3. Insert a new pending row + mint a 6-digit OTP, hash it.
    4. Send the OTP to the *new* email via the email adapter.

    Returns ``EmailChangeStarted`` on success or ``EmailChangeFailed``
    with ``reason='email_in_use'`` if the uniqueness check fails. The
    portal route renders both branches.

    The caller commits. If the commit fails, no OTP token is left
    around (the row never landed); the email adapter side-effect
    (already sent) is harmless because no row will validate the OTP.
    """
    new_email_normalized = new_email.strip().lower()

    # Up-front uniqueness check: any active (valid_to IS NULL) email
    # contact_medium with this value blocks the change. The unique
    # index also enforces this at commit time (defense in depth).
    existing = (
        await db.execute(
            select(ContactMedium.id)
            .where(
                ContactMedium.medium_type == "email",
                ContactMedium.value == new_email_normalized,
                ContactMedium.valid_to.is_(None),
            )
        )
    ).first()
    if existing is not None:
        return EmailChangeFailed(reason="email_in_use")

    # Void any prior pending row.
    await db.execute(
        update(EmailChangePending)
        .where(
            EmailChangePending.identity_id == identity_id,
            EmailChangePending.status == "pending",
        )
        .values(status="cancelled")
    )

    now = clock_now()
    otp = generate_otp()
    pending_id = _login_token_id().replace("LTK-", "ECP-")

    db.add(
        EmailChangePending(
            id=pending_id,
            identity_id=identity_id,
            new_email=new_email_normalized,
            code_hash=hash_token(otp),
            issued_at=now,
            # 24-hour expiry per V0_10_0.md §7.2.
            expires_at=now + timedelta(hours=24),
            status="pending",
            ip=ip,
            user_agent=user_agent,
        )
    )
    await db.flush()

    # Side-effect: the OTP goes to the NEW email. The customer cannot
    # complete the verify step without access to the new mailbox —
    # which is the whole point.
    email_adapter.send_step_up(new_email_normalized, otp, "email_change")

    return EmailChangeStarted(pending_id=pending_id, new_email=new_email_normalized)


# ── verify_email_change — the cross-schema atomic write ──────────────────


async def verify_email_change(
    db: AsyncSession,
    *,
    identity_id: str,
    code: str,
) -> EmailChangeApplied | EmailChangeFailed:
    """Verify the OTP and atomically commit the email change.

    All four operations land in the same ``AsyncSession`` — i.e. the
    same Postgres transaction — so either all four succeed and the
    caller commits, or none of them stick:

    1. Match the OTP against the active pending row for this identity.
       Code mismatch / expired / no-row → return ``EmailChangeFailed``
       *before* writing anything.
    2. Find the customer linked to this identity, then the party's
       active (``valid_to IS NULL``) email contact_medium. Update its
       ``value`` to the new email.
    3. Update ``portal_auth.identity.email`` to the new email.
    4. Mark the pending row ``status='consumed', consumed_at=now``.

    The caller is expected to ``await db.commit()`` after this returns
    ``EmailChangeApplied``. On any of the failure branches the caller
    must ``rollback`` (or just commit the no-op state — no writes
    happened).

    The cross-schema write is the doctrine bend documented in
    DECISIONS 2026-04-27. The justification is that splitting the
    update across two HTTP calls (CRM, then portal_auth) leaves a
    half-committed state if the second call fails, which is exactly
    what V0_10_0.md "Do not silently downgrade the email-change
    flow's atomicity" forbids.
    """
    # ── Step 1: validate the OTP ─────────────────────────────────────
    pending = (
        await db.execute(
            select(EmailChangePending).where(
                EmailChangePending.identity_id == identity_id,
                EmailChangePending.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if pending is None:
        return EmailChangeFailed(reason="no_active_pending")

    now = clock_now()
    if pending.expires_at <= now:
        # Mark expired so the partial-unique index lets a fresh row in.
        pending.status = "expired"
        await db.flush()
        return EmailChangeFailed(reason="expired")

    if not verify_token(code.strip(), pending.code_hash):
        return EmailChangeFailed(reason="wrong_code")

    # ── Step 2: update CRM contact_medium ────────────────────────────
    identity = (
        await db.execute(select(Identity).where(Identity.id == identity_id))
    ).scalar_one_or_none()
    if identity is None or identity.customer_id is None:
        # Identity exists but isn't linked — should be impossible at
        # this stage (the route gates on requires_linked_customer)
        # but defensive.
        return EmailChangeFailed(reason="no_active_pending")

    customer = (
        await db.execute(
            select(Customer).where(Customer.id == identity.customer_id)
        )
    ).scalar_one_or_none()
    if customer is None:
        return EmailChangeFailed(reason="no_active_pending")

    # Find the active email row on this party.
    cm = (
        await db.execute(
            select(ContactMedium).where(
                ContactMedium.party_id == customer.party_id,
                ContactMedium.medium_type == "email",
                ContactMedium.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()

    if cm is None:
        # Edge case: no active email row at all (shouldn't happen for a
        # linked customer, but we don't silently fabricate one — that
        # is a CSR ticket, not a self-serve change).
        return EmailChangeFailed(reason="no_active_pending")

    cm.value = pending.new_email
    await db.flush()

    # ── Step 3: update portal_auth.identity.email ────────────────────
    identity.email = pending.new_email
    await db.flush()

    # ── Step 4: mark pending consumed ────────────────────────────────
    pending.status = "consumed"
    pending.consumed_at = now
    await db.flush()

    return EmailChangeApplied(new_email=pending.new_email)


# ── cancel_pending_email_change ──────────────────────────────────────────


async def cancel_pending_email_change(
    db: AsyncSession, *, identity_id: str
) -> bool:
    """Cancel the active pending row, if any. Returns True iff one was cancelled."""
    res = await db.execute(
        update(EmailChangePending)
        .where(
            EmailChangePending.identity_id == identity_id,
            EmailChangePending.status == "pending",
        )
        .values(status="cancelled")
    )
    await db.flush()
    return (res.rowcount or 0) > 0
