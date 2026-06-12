"""bss-portal-auth — email-based portal identity for the self-serve portal.

v0.8 surface (V0_8_0.md §1.3):

* ``start_email_login`` — issue OTP + magic-link, hand off to email adapter.
* ``verify_email_login`` — verify OTP/magic-link, mint a session.
* ``current_session`` — resolve cookie -> (session, identity), bump last_seen_at.
* ``rotate_if_due`` — sliding cookie rotation past TTL/2.
* ``revoke_session`` — explicit logout.
* ``link_to_customer`` — bind identity to customer at first paid signup.
* ``start_step_up`` / ``verify_step_up`` / ``consume_step_up_token`` —
  one-shot per-action elevation.

Plus:

* ``validate_pepper_present`` — startup hook for the portal lifespan.
* ``Settings`` — env-driven config.
* Email adapters (``LoggingEmailAdapter``, ``NoopEmailAdapter``,
  ``select_adapter``).
* Test helpers (``test_helpers.create_test_session``,
  ``test_helpers.last_login_codes``).

Phase 12 will swap ``current_session`` for OAuth2/JWT-based identity.
The auth flows here will then become the portal's local-account
fallback; the dataclasses (``IdentityView``, ``SessionView``,
``StepUpToken``) keep their shape.
"""

from .audit import record_portal_action
from .config import Settings
from .email import (
    EmailAdapter,
    LoggingEmailAdapter,
    NoopEmailAdapter,
    SmtpEmailAdapter,
    select_adapter,
)
from .email_change import (
    EmailChangeApplied,
    EmailChangeFailed,
    EmailChangeStarted,
    cancel_pending_email_change,
    start_email_change,
    verify_email_change,
)
from .pending_action import (
    PendingActionView,
    consume_pending_action,
    stash_pending_action,
)
from .service import (
    consume_step_up_token,
    current_session,
    link_to_customer,
    revoke_session,
    rotate_if_due,
    start_email_login,
    start_step_up,
    verify_email_login,
    verify_step_up,
)
from .startup import validate_pepper_present
from .types import (
    IdentityView,
    LoginChallenge,
    LoginFailed,
    RateLimitExceeded,
    SessionView,
    StepUpChallenge,
    StepUpFailed,
    StepUpToken,
)

__all__ = [
    # Config + startup
    "Settings",
    "validate_pepper_present",
    # Email adapters
    "EmailAdapter",
    "LoggingEmailAdapter",
    "NoopEmailAdapter",
    "SmtpEmailAdapter",
    "select_adapter",
    # Public dataclasses
    "IdentityView",
    "SessionView",
    "LoginChallenge",
    "LoginFailed",
    "StepUpChallenge",
    "StepUpToken",
    "StepUpFailed",
    "RateLimitExceeded",
    # Service surface
    "start_email_login",
    "verify_email_login",
    "current_session",
    "rotate_if_due",
    "revoke_session",
    "link_to_customer",
    "start_step_up",
    "verify_step_up",
    "consume_step_up_token",
    # v0.10 — portal-side per-write audit
    "record_portal_action",
    # v0.10 PR 8 — cross-schema atomic email change
    "start_email_change",
    "verify_email_change",
    "cancel_pending_email_change",
    "EmailChangeStarted",
    "EmailChangeApplied",
    "EmailChangeFailed",
    # v0.12 — POST-body stash for step-up replay
    "stash_pending_action",
    "consume_pending_action",
    "PendingActionView",
]
