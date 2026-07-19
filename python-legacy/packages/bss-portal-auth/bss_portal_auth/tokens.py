"""Token primitives — generation, hashing, timing-safe comparison.

Doctrine (V0_8_0.md §1.5):

* OTP — 6 numeric digits via ``secrets.choice``.
* Magic link — 32-char URL-safe via ``secrets.token_urlsafe(24)``.
* Stored as HMAC-SHA-256 of (token || pepper). Pepper from
  ``BSS_PORTAL_TOKEN_PEPPER`` env, ≥32 chars, validated at startup
  via ``validate_pepper_present``.
* Comparison is ``hmac.compare_digest`` — timing-safe.

This module is pure-Python — no DB, no ORM. The session-binding logic
lives in ``service.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from .config import Settings

OTP_LENGTH = 6
_MAGIC_LINK_BYTES = 24  # secrets.token_urlsafe(24) -> 32 chars URL-safe


def generate_otp() -> str:
    """Return a 6-digit numeric OTP, generated via ``secrets`` (not ``random``)."""
    return "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))


def generate_magic_link_token() -> str:
    """Return a 32-char URL-safe token suitable for ``?token=`` in a magic link."""
    return secrets.token_urlsafe(_MAGIC_LINK_BYTES)


def generate_session_id() -> str:
    """Return a 32-char URL-safe opaque session id (cookie value)."""
    return secrets.token_urlsafe(_MAGIC_LINK_BYTES)


def generate_step_up_grant() -> str:
    """One-shot token returned to the portal after a successful step-up verify.

    Stored hashed (same pepper); the portal forwards it on the next
    sensitive request as ``X-BSS-StepUp-Token`` and the route handler
    consumes it via ``consume_step_up_token``.
    """
    return secrets.token_urlsafe(_MAGIC_LINK_BYTES)


def hash_token(token: str, *, pepper: str | None = None) -> str:
    """HMAC-SHA-256 of ``token`` keyed by the server pepper.

    Hex-encoded for predictable column type. ``pepper=None`` reads the
    Settings value — explicit override is a test convenience.
    """
    if pepper is None:
        pepper = Settings().BSS_PORTAL_TOKEN_PEPPER
    if not pepper:
        # Defensive — startup validator should have caught this. Raising
        # here means a regression from the validator can't silently
        # downgrade to "all tokens hash to the same value".
        raise RuntimeError(
            "BSS_PORTAL_TOKEN_PEPPER missing — call validate_pepper_present() "
            "in lifespan startup before any auth flow runs."
        )
    return hmac.new(
        pepper.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_token(token: str, expected_hash: str, *, pepper: str | None = None) -> bool:
    """Timing-safe verify: hash ``token`` and compare to ``expected_hash``.

    Returns True iff the hashes are equal under ``hmac.compare_digest``.
    Always uses constant-time comparison — never short-circuits on
    length, never uses ``==``.
    """
    actual = hash_token(token, pepper=pepper)
    return hmac.compare_digest(actual, expected_hash)
