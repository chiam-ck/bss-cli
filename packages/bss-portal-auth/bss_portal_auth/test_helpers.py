"""Test helpers — DO NOT IMPORT FROM PRODUCTION CODE.

Tests use these to:

* Spin up an in-process ``NoopEmailAdapter`` whose records are
  inspectable via ``last_login_codes(adapter, email)``.
* Mint a session for a verified identity bypassing the email flow,
  via ``create_test_session``. The portal route tests use this so they
  don't have to drive the full magic-link flow for every assertion.

Production callers go through ``service.py``. Anything in this module
that escapes into a route handler is a bug.
"""

from __future__ import annotations

from datetime import timedelta

from bss_clock import now as clock_now
from bss_models import Identity, LoginToken, Session
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .email import NoopEmailAdapter
from .service import _identity_id, _login_token_id, _to_identity_view, _to_session_view
from .tokens import generate_session_id, generate_step_up_grant, hash_token
from .types import IdentityView, SessionView


def last_login_codes(adapter: NoopEmailAdapter, email: str) -> dict[str, str]:
    """Return the most recent login OTP + magic_link for ``email``.

    Empty dict if no login was sent. Used by the portal route tests to
    pluck the OTP without tailing the dev mailbox file.
    """
    rec = adapter.records.get((email, "login"))
    if rec is None:
        return {}
    return {"otp": str(rec["otp"]), "magic_link": str(rec["magic_link"])}


def last_step_up_code(
    adapter: NoopEmailAdapter, email: str, action_label: str
) -> str | None:
    """Return the most recent step-up OTP for ``(email, action_label)``."""
    rec = adapter.records.get((email, f"step_up:{action_label}"))
    if rec is None:
        return None
    return str(rec["otp"])


async def create_test_session(
    db: AsyncSession,
    *,
    email: str,
    customer_id: str | None = None,
    verified: bool = True,
) -> tuple[SessionView, IdentityView]:
    """Mint a session for a freshly-created identity in one call.

    ``verified=True`` stamps ``email_verified_at`` and bumps status,
    matching what a successful magic-link flow would produce. Tests use
    this to skip the email round-trip when asserting middleware /
    route gating.
    """
    settings = Settings()
    now = clock_now()
    identity = Identity(
        id=_identity_id(),
        email=email,
        customer_id=customer_id,
        email_verified_at=now if verified else None,
        status=("registered" if customer_id else ("verified" if verified else "unverified")),
        created_at=now,
        last_login_at=now if verified else None,
    )
    db.add(identity)
    await db.flush()

    sess = Session(
        id=generate_session_id(),
        identity_id=identity.id,
        issued_at=now,
        expires_at=now + timedelta(seconds=settings.BSS_PORTAL_SESSION_TTL_S),
        last_seen_at=now,
    )
    db.add(sess)
    await db.flush()
    return _to_session_view(sess), _to_identity_view(identity)


async def mint_step_up_grant(
    db: AsyncSession,
    *,
    session_id: str,
    action_label: str,
) -> str:
    """Insert a fresh ``step_up_grant`` row + return the plaintext token.

    v0.10 — post-login route tests need a valid grant cookie to exercise
    the ``requires_step_up`` path without driving the full OTP flow each
    time. Production callers reach this state by calling
    ``verify_step_up`` after the customer enters the OTP they received
    via the email adapter; tests skip the round-trip.

    The hashed token is what's stored; the plaintext is what the test
    sets as the ``bss_portal_step_up`` cookie. ``consume_step_up_token``
    reads the cookie and matches against the stored hash + action_label.
    """
    settings = Settings()
    sess = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()

    now = clock_now()
    grant = generate_step_up_grant()
    db.add(
        LoginToken(
            id=_login_token_id(),
            identity_id=sess.identity_id,
            kind="step_up_grant",
            code_hash=hash_token(grant),
            action_label=action_label,
            issued_at=now,
            expires_at=now + timedelta(seconds=settings.BSS_PORTAL_STEPUP_GRANT_TTL_S),
        )
    )
    await db.flush()
    return grant
