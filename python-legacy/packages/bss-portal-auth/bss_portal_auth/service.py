"""Public service layer — the eight functions the portal calls.

Doctrine reminders (V0_8_0.md):

* No passwords. Magic link or OTP only.
* Tokens stored as HMAC-SHA-256 with server pepper, never plaintext.
* Comparison is timing-safe (``hmac.compare_digest``).
* ``bss_clock.now()`` for every time-sensitive operation.
* Verification failures distinct in the audit log
  ('wrong_code' / 'expired' / 'no_active_token') but the
  customer-facing response is intentionally generic.

Every public function takes an ``AsyncSession`` so the portal owns
transaction boundaries. The portal is expected to commit after a
successful call; the service layer flushes (so ids are stable) but
does not commit on its own.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import quote

from bss_clock import now as clock_now
from bss_models import Identity, LoginAttempt, LoginToken, Session
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .email import EmailAdapter
from .rate_limit import (
    enforce_login_start,
    enforce_login_verify,
    enforce_step_up_start,
)
from .tokens import (
    generate_magic_link_token,
    generate_otp,
    generate_session_id,
    generate_step_up_grant,
    hash_token,
    verify_token,
)
from .types import (
    IdentityView,
    LoginChallenge,
    LoginFailed,
    SessionView,
    StepUpChallenge,
    StepUpFailed,
    StepUpToken,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _identity_id() -> str:
    return f"IDN-{secrets.token_hex(8)}"


def _login_token_id() -> str:
    return f"LTK-{secrets.token_hex(8)}"


def _to_identity_view(row: Identity) -> IdentityView:
    return IdentityView(
        id=row.id,
        email=row.email,
        customer_id=row.customer_id,
        email_verified_at=row.email_verified_at,
        status=row.status,
    )


def _to_session_view(row: Session) -> SessionView:
    return SessionView(
        id=row.id,
        identity_id=row.identity_id,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        last_seen_at=row.last_seen_at,
    )


async def _record_attempt(
    db: AsyncSession,
    *,
    email: str | None,
    ip: str | None,
    stage: str,
    outcome: str,
) -> None:
    """Append an audit row. Called on every code path — success or failure."""
    db.add(
        LoginAttempt(
            email=email,
            ip=ip,
            ts=clock_now(),
            outcome=outcome,
            stage=stage,
        )
    )
    await db.flush()


# ── magic-link URL builder ───────────────────────────────────────────────


def _build_magic_link_url(public_url: str, *, email: str, token: str) -> str:
    """Build the ``/auth/verify`` URL for the magic-link click-through.

    If ``public_url`` is empty, returns the bare token — preserves the
    v0.8 LoggingEmailAdapter behavior where operators read tokens out
    of the dev mailbox file and paste them manually. Production
    deployments using ResendEmailAdapter (or future SmtpEmailAdapter)
    must set ``BSS_PORTAL_PUBLIC_URL`` so real mail clients can render
    a clickable link instead of mangling the bare token (Apple Mail
    rewrites bare tokens as ``x-webdoc://...``).
    """
    if not public_url:
        return token
    base = public_url.rstrip("/")
    return f"{base}/auth/verify?email={quote(email)}&token={quote(token)}"


# ── start_email_login ────────────────────────────────────────────────────


async def start_email_login(
    db: AsyncSession,
    *,
    email: str,
    ip: str | None = None,
    user_agent: str | None = None,
    email_adapter: EmailAdapter,
) -> LoginChallenge:
    """Mint OTP + magic-link, store hashed, hand off to email adapter.

    Idempotent on the identity: a known email re-uses its row. Any
    previously-issued un-consumed login tokens are NOT revoked here —
    they expire on their own. Multiple in-flight tokens make the
    "I clicked the previous link" recovery less surprising.
    """
    settings = Settings()

    await enforce_login_start(db, email=email, ip=ip)

    identity = (
        await db.execute(select(Identity).where(Identity.email == email))
    ).scalar_one_or_none()
    if identity is None:
        identity = Identity(
            id=_identity_id(),
            email=email,
            status="unverified",
            created_at=clock_now(),
        )
        db.add(identity)
        await db.flush()

    otp = generate_otp()
    magic = generate_magic_link_token()
    issued = clock_now()
    expires = issued + timedelta(seconds=settings.BSS_PORTAL_LOGIN_TOKEN_TTL_S)

    db.add(
        LoginToken(
            id=_login_token_id(),
            identity_id=identity.id,
            kind="otp",
            code_hash=hash_token(otp),
            issued_at=issued,
            expires_at=expires,
            ip=ip,
            user_agent=user_agent,
        )
    )
    db.add(
        LoginToken(
            id=_login_token_id(),
            identity_id=identity.id,
            kind="magic_link",
            code_hash=hash_token(magic),
            issued_at=issued,
            expires_at=expires,
            ip=ip,
            user_agent=user_agent,
        )
    )

    await _record_attempt(
        db, email=email, ip=ip, stage="login_start", outcome="issued"
    )
    await db.flush()

    # Build the click-through URL. v0.14: bare-token magic-link broke
    # in real mail clients (Apple Mail rendered as ``x-webdoc://...``);
    # ResendEmailAdapter exposes the bug because it embeds the value
    # in HTML href. The base URL must be set for any
    # non-Logging/Noop adapter; LoggingEmailAdapter falls back to the
    # bare token (operators paste manually anyway).
    magic_link_url = _build_magic_link_url(
        settings.BSS_PORTAL_PUBLIC_URL, email=email, token=magic
    )

    # Hand off plaintext to the email adapter — never written anywhere
    # else, never logged. The adapter is responsible for getting it to
    # the customer (file in dev, real SMTP/Resend in prod).
    email_adapter.send_login(email, otp, magic_link_url)

    return LoginChallenge(identity_id=identity.id, expires_at=expires)


# ── verify_email_login ───────────────────────────────────────────────────


async def verify_email_login(
    db: AsyncSession,
    *,
    email: str,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> SessionView | LoginFailed:
    """Verify OTP or magic-link `code` against active tokens for `email`.

    On success: marks the matched token consumed, mints a session,
    stamps `email_verified_at` if first-time, updates `last_login_at`.
    On failure: records a structured outcome in `login_attempt`. The
    portal renders the same generic message regardless of which
    failure was hit.
    """
    settings = Settings()
    await enforce_login_verify(db, email=email)

    identity = (
        await db.execute(select(Identity).where(Identity.email == email))
    ).scalar_one_or_none()
    if identity is None:
        await _record_attempt(
            db,
            email=email,
            ip=ip,
            stage="login_verify",
            outcome="no_such_identity",
        )
        return LoginFailed(reason="no_such_identity")

    now = clock_now()

    # Active tokens for this identity, OTP or magic_link, unconsumed,
    # unexpired. We pull them all and compare via timing-safe verify.
    rows = (
        await db.execute(
            select(LoginToken).where(
                LoginToken.identity_id == identity.id,
                LoginToken.kind.in_(["otp", "magic_link"]),
                LoginToken.consumed_at.is_(None),
            )
        )
    ).scalars().all()

    if not rows:
        await _record_attempt(
            db,
            email=email,
            ip=ip,
            stage="login_verify",
            outcome="no_active_token",
        )
        return LoginFailed(reason="no_active_token")

    matched: LoginToken | None = None
    any_unexpired = False
    for row in rows:
        if row.expires_at <= now:
            continue
        any_unexpired = True
        if verify_token(code, row.code_hash):
            matched = row
            break

    if matched is None:
        if not any_unexpired:
            outcome = "expired"
        else:
            outcome = "wrong_code"
        await _record_attempt(
            db, email=email, ip=ip, stage="login_verify", outcome=outcome
        )
        return LoginFailed(reason=outcome)

    matched.consumed_at = now

    # v0.10 fix — auto-link a verified identity to a pre-existing CRM
    # customer if the email matches exactly one active email contact
    # medium. Returning customers whose CRM record predates the portal
    # identity (e.g. created via CLI or imported) would otherwise log
    # in to an empty dashboard because their identity_id has
    # customer_id=NULL until the signup funnel's harvest hook runs.
    # The crm.contact_medium uniqueness invariant guarantees at most
    # one match, so the lookup is safe.
    if identity.customer_id is None:
        from bss_models import ContactMedium, Customer

        cm = (
            await db.execute(
                select(ContactMedium).where(
                    ContactMedium.medium_type == "email",
                    ContactMedium.value == email,
                    ContactMedium.valid_to.is_(None),
                )
            )
        ).scalar_one_or_none()
        if cm is not None:
            cust = (
                await db.execute(
                    select(Customer).where(Customer.party_id == cm.party_id)
                )
            ).scalar_one_or_none()
            if cust is not None:
                identity.customer_id = cust.id

    if identity.email_verified_at is None:
        identity.email_verified_at = now
        identity.status = "registered" if identity.customer_id else "verified"
    elif identity.customer_id is not None and identity.status != "registered":
        # Auto-link just bound the identity — promote status accordingly so
        # the session middleware can route the customer to the dashboard.
        identity.status = "registered"
    identity.last_login_at = now

    sess = Session(
        id=generate_session_id(),
        identity_id=identity.id,
        issued_at=now,
        expires_at=now + timedelta(seconds=settings.BSS_PORTAL_SESSION_TTL_S),
        last_seen_at=now,
        ip=ip,
        user_agent=user_agent,
    )
    db.add(sess)

    await _record_attempt(
        db, email=email, ip=ip, stage="login_verify", outcome="success"
    )
    await db.flush()
    return _to_session_view(sess)


# ── current_session / revoke_session ─────────────────────────────────────


async def current_session(
    db: AsyncSession, cookie_value: str
) -> tuple[SessionView, IdentityView] | None:
    """Resolve a cookie to (session, identity) and bump ``last_seen_at``.

    Returns None if revoked, expired, or the cookie doesn't match a row.
    Does NOT rotate the session — sliding rotation is the middleware's
    job (see ``rotate_if_due``).
    """
    if not cookie_value:
        return None

    sess = (
        await db.execute(select(Session).where(Session.id == cookie_value))
    ).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        return None
    now = clock_now()
    if sess.expires_at <= now:
        return None

    identity = (
        await db.execute(select(Identity).where(Identity.id == sess.identity_id))
    ).scalar_one_or_none()
    if identity is None or identity.status == "deleted":
        return None

    sess.last_seen_at = now
    await db.flush()
    return _to_session_view(sess), _to_identity_view(identity)


async def rotate_if_due(
    db: AsyncSession, session_id: str
) -> SessionView | None:
    """If the session has aged past TTL/2, mint a new id and revoke the old.

    Called by the middleware on every request after ``current_session``
    succeeds. Halfway through the TTL is the inflection — keeps cookies
    rotating while the customer is active without rotating on every
    single request. Returns the NEW session, or None if no rotation.
    """
    settings = Settings()
    now = clock_now()
    sess = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        return None

    age = (now - sess.issued_at).total_seconds()
    if age < settings.BSS_PORTAL_SESSION_TTL_S / 2:
        return None

    new = Session(
        id=generate_session_id(),
        identity_id=sess.identity_id,
        issued_at=now,
        expires_at=now + timedelta(seconds=settings.BSS_PORTAL_SESSION_TTL_S),
        last_seen_at=now,
        ip=sess.ip,
        user_agent=sess.user_agent,
    )
    db.add(new)
    sess.revoked_at = now
    await db.flush()
    return _to_session_view(new)


async def revoke_session(db: AsyncSession, session_id: str) -> None:
    """Explicit logout — set ``revoked_at = now()``. Idempotent."""
    now = clock_now()
    await db.execute(
        update(Session)
        .where(Session.id == session_id, Session.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.flush()


# ── link_to_customer ─────────────────────────────────────────────────────


async def link_to_customer(
    db: AsyncSession, *, identity_id: str, customer_id: str
) -> IdentityView:
    """Bind an identity to a customer at the moment of first paid signup.

    Idempotent: re-calling with the same (identity, customer) pair is a
    no-op. Calling with a different customer when one is already linked
    raises — links are 1:1 and not reassignable from this surface.
    Soft-delete + re-signup is the only path to a different customer.
    """
    identity = (
        await db.execute(select(Identity).where(Identity.id == identity_id))
    ).scalar_one_or_none()
    if identity is None:
        raise ValueError(f"unknown identity: {identity_id}")

    if identity.customer_id is not None:
        if identity.customer_id == customer_id:
            return _to_identity_view(identity)
        raise ValueError(
            f"identity {identity_id} already linked to customer "
            f"{identity.customer_id}; cannot relink to {customer_id}"
        )

    identity.customer_id = customer_id
    identity.status = "registered"
    await db.flush()
    return _to_identity_view(identity)


# ── start_step_up / verify_step_up / consume_step_up_token ───────────────


async def start_step_up(
    db: AsyncSession,
    *,
    session_id: str,
    action_label: str,
    ip: str | None = None,
    user_agent: str | None = None,
    email_adapter: EmailAdapter,
) -> StepUpChallenge:
    """Issue a fresh OTP scoped to ``action_label``."""
    settings = Settings()
    await enforce_step_up_start(db, session_id=session_id)

    sess = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        raise ValueError("session not found or revoked")

    identity = (
        await db.execute(select(Identity).where(Identity.id == sess.identity_id))
    ).scalar_one_or_none()
    if identity is None:
        raise ValueError("identity gone")

    otp = generate_otp()
    issued = clock_now()
    expires = issued + timedelta(seconds=settings.BSS_PORTAL_STEPUP_TOKEN_TTL_S)

    db.add(
        LoginToken(
            id=_login_token_id(),
            identity_id=identity.id,
            kind="step_up",
            code_hash=hash_token(otp),
            action_label=action_label,
            issued_at=issued,
            expires_at=expires,
            ip=ip,
            user_agent=user_agent,
        )
    )
    await _record_attempt(
        db,
        email=identity.email,
        ip=f"session:{session_id}",
        stage="step_up_start",
        outcome="issued",
    )
    await db.flush()
    email_adapter.send_step_up(identity.email, otp, action_label)

    return StepUpChallenge(
        session_id=session_id,
        action_label=action_label,
        expires_at=expires,
    )


async def verify_step_up(
    db: AsyncSession,
    *,
    session_id: str,
    code: str,
    action_label: str,
) -> StepUpToken | StepUpFailed:
    """Match OTP against an active step_up token scoped to action_label.

    On success: mark the OTP token consumed, mint a one-shot grant
    token (hashed and stored as a *new* row with the same action_label
    so ``consume_step_up_token`` can find and consume it). The grant
    expires in ``BSS_PORTAL_STEPUP_GRANT_TTL_S`` seconds.
    """
    settings = Settings()
    sess = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        return StepUpFailed(reason="no_active_token")

    rows = (
        await db.execute(
            select(LoginToken).where(
                LoginToken.identity_id == sess.identity_id,
                LoginToken.kind == "step_up",
                LoginToken.action_label == action_label,
                LoginToken.consumed_at.is_(None),
            )
        )
    ).scalars().all()

    now = clock_now()
    if not rows:
        return StepUpFailed(reason="no_active_token")

    matched: LoginToken | None = None
    any_unexpired = False
    for row in rows:
        if row.expires_at <= now:
            continue
        any_unexpired = True
        if verify_token(code, row.code_hash):
            matched = row
            break

    if matched is None:
        return StepUpFailed(
            reason="expired" if not any_unexpired else "wrong_code"
        )

    matched.consumed_at = now

    grant = generate_step_up_grant()
    grant_expires = now + timedelta(seconds=settings.BSS_PORTAL_STEPUP_GRANT_TTL_S)
    db.add(
        LoginToken(
            id=_login_token_id(),
            identity_id=sess.identity_id,
            kind="step_up_grant",
            code_hash=hash_token(grant),
            action_label=action_label,
            issued_at=now,
            expires_at=grant_expires,
        )
    )
    await db.flush()
    return StepUpToken(
        token=grant,
        expires_at=grant_expires,
        action_label=action_label,
    )


async def consume_step_up_token(
    db: AsyncSession, *, session_id: str, token: str, action_label: str
) -> bool:
    """Validate + atomically consume a one-shot step-up grant.

    Called by route handlers at the moment of a sensitive write.
    Returns True iff a matching, unconsumed, unexpired grant for the
    same action_label exists; the row is marked consumed in the same
    flush so a second call returns False.
    """
    sess = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if sess is None or sess.revoked_at is not None:
        return False

    rows = (
        await db.execute(
            select(LoginToken).where(
                LoginToken.identity_id == sess.identity_id,
                LoginToken.kind == "step_up_grant",
                LoginToken.action_label == action_label,
                LoginToken.consumed_at.is_(None),
            )
        )
    ).scalars().all()

    now = clock_now()
    for row in rows:
        if row.expires_at <= now:
            continue
        if verify_token(token, row.code_hash):
            row.consumed_at = now
            await db.flush()
            return True
    return False
