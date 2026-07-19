"""Brute-force rate limits — read from ``portal_auth.login_attempt``.

The login_attempt table is append-only. For each rate-limit check we
count rows in the relevant (key, stage, window) and raise if over the
cap. ``ts`` is the source of truth — clock-driven, so frozen-clock
scenarios behave deterministically.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from bss_clock import now as clock_now
from bss_models import LoginAttempt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .types import RateLimitExceeded


async def _count_in_window(
    db: AsyncSession,
    *,
    column,
    value: str,
    stage: str,
    window_s: int,
    until: datetime,
) -> tuple[int, datetime | None]:
    """Return (count, oldest_ts) of attempts matching key+stage in window."""
    cutoff = until - timedelta(seconds=window_s)
    stmt = select(
        func.count(LoginAttempt.id),
        func.min(LoginAttempt.ts),
    ).where(
        column == value,
        LoginAttempt.stage == stage,
        LoginAttempt.ts >= cutoff,
    )
    result = await db.execute(stmt)
    count, oldest = result.one()
    return int(count or 0), oldest


async def enforce_login_start(db: AsyncSession, *, email: str, ip: str | None) -> None:
    """Raise ``RateLimitExceeded`` if email or IP exceeds the start cap."""
    settings = Settings()
    now = clock_now()

    count_e, oldest_e = await _count_in_window(
        db,
        column=LoginAttempt.email,
        value=email,
        stage="login_start",
        window_s=settings.BSS_PORTAL_LOGIN_PER_EMAIL_WINDOW_S,
        until=now,
    )
    if count_e >= settings.BSS_PORTAL_LOGIN_PER_EMAIL_MAX:
        retry = _retry_after_s(oldest_e, now, settings.BSS_PORTAL_LOGIN_PER_EMAIL_WINDOW_S)
        raise RateLimitExceeded(retry_after_seconds=retry, scope="login_start_per_email")

    if ip is not None:
        count_i, oldest_i = await _count_in_window(
            db,
            column=LoginAttempt.ip,
            value=ip,
            stage="login_start",
            window_s=settings.BSS_PORTAL_LOGIN_PER_IP_WINDOW_S,
            until=now,
        )
        if count_i >= settings.BSS_PORTAL_LOGIN_PER_IP_MAX:
            retry = _retry_after_s(oldest_i, now, settings.BSS_PORTAL_LOGIN_PER_IP_WINDOW_S)
            raise RateLimitExceeded(retry_after_seconds=retry, scope="login_start_per_ip")


async def enforce_login_verify(db: AsyncSession, *, email: str) -> None:
    """Raise ``RateLimitExceeded`` if the email has too many recent verify attempts."""
    settings = Settings()
    now = clock_now()
    count, oldest = await _count_in_window(
        db,
        column=LoginAttempt.email,
        value=email,
        stage="login_verify",
        window_s=settings.BSS_PORTAL_VERIFY_PER_EMAIL_WINDOW_S,
        until=now,
    )
    if count >= settings.BSS_PORTAL_VERIFY_PER_EMAIL_MAX:
        retry = _retry_after_s(oldest, now, settings.BSS_PORTAL_VERIFY_PER_EMAIL_WINDOW_S)
        raise RateLimitExceeded(retry_after_seconds=retry, scope="login_verify_per_email")


async def enforce_step_up_start(db: AsyncSession, *, session_id: str) -> None:
    """Per-session step-up cap — keyed on session_id stored in `ip` column.

    Reusing the ``ip`` column for a session-scoped key is a deliberate
    simplification: login_attempt is a flat audit log, and step_up
    rate limits are per-session by definition. The ``stage`` discriminator
    keeps it from colliding with real IP-based caps.
    """
    settings = Settings()
    now = clock_now()
    count, oldest = await _count_in_window(
        db,
        column=LoginAttempt.ip,
        value=f"session:{session_id}",
        stage="step_up_start",
        window_s=settings.BSS_PORTAL_STEPUP_PER_SESSION_WINDOW_S,
        until=now,
    )
    if count >= settings.BSS_PORTAL_STEPUP_PER_SESSION_MAX:
        retry = _retry_after_s(oldest, now, settings.BSS_PORTAL_STEPUP_PER_SESSION_WINDOW_S)
        raise RateLimitExceeded(retry_after_seconds=retry, scope="step_up_per_session")


def _retry_after_s(oldest: datetime | None, now: datetime, window_s: int) -> int:
    """Seconds until the oldest in-window attempt rolls off."""
    if oldest is None:
        return window_s
    elapsed = (now - oldest).total_seconds()
    return max(int(window_s - elapsed), 1)
