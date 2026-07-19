"""Public dataclasses returned from the auth service surface.

Deliberately small, frozen, free of ORM types — callers of the
``bss_portal_auth`` API should never see SQLAlchemy models. The DB
layer ferries Identity/Session/LoginToken rows; this module is what
the portal code touches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LoginChallenge:
    """Returned by ``start_email_login`` — non-secret state only."""

    identity_id: str
    expires_at: datetime


@dataclass(frozen=True)
class IdentityView:
    """Read-only projection of an identity row."""

    id: str
    email: str
    customer_id: str | None
    email_verified_at: datetime | None
    status: str


@dataclass(frozen=True)
class SessionView:
    """Read-only projection of a session row."""

    id: str
    identity_id: str
    issued_at: datetime
    expires_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class StepUpChallenge:
    """Returned by ``start_step_up`` — non-secret state only."""

    session_id: str
    action_label: str
    expires_at: datetime


@dataclass(frozen=True)
class StepUpToken:
    """One-shot grant returned by ``verify_step_up``.

    ``token`` is the plaintext value the portal forwards on the next
    sensitive request; the route handler consumes it via
    ``consume_step_up_token`` which marks the underlying row consumed.
    Stored hashed; the plaintext only exists in this in-memory object.
    """

    token: str
    expires_at: datetime
    action_label: str


# ── Failure shapes (return values, not exceptions, for ergonomic flow) ────


@dataclass(frozen=True)
class LoginFailed:
    """Verify did not produce a session. ``reason`` is a stable token."""

    reason: str  # 'wrong_code' | 'expired' | 'no_active_token' | 'no_such_identity'


@dataclass(frozen=True)
class StepUpFailed:
    """Step-up verify did not produce a token."""

    reason: str  # 'wrong_code' | 'expired' | 'no_active_token' | 'wrong_action'


# ── Exceptions reserved for blocking conditions, not flow control ────────


class RateLimitExceeded(Exception):
    """Raised by ``start_email_login`` / ``verify_email_login`` /
    ``start_step_up`` when the configured per-email / per-IP /
    per-session window is exceeded.

    ``retry_after_seconds`` is the wait time before the *oldest* attempt
    in the window expires. The portal surfaces a generic "too many
    attempts, try again later" copy; the seconds value is for log /
    debugging context, not for displaying to the customer.
    """

    def __init__(self, *, retry_after_seconds: int, scope: str):
        super().__init__(
            f"rate limit exceeded for {scope} — retry in {retry_after_seconds}s"
        )
        self.retry_after_seconds = retry_after_seconds
        self.scope = scope
