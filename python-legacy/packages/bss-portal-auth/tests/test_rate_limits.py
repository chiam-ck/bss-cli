"""Rate limits — per-email login starts, per-IP login starts, per-email verify."""

from __future__ import annotations

import pytest
from bss_portal_auth import (
    RateLimitExceeded,
    start_email_login,
    verify_email_login,
)


@pytest.mark.asyncio
async def test_login_start_per_email_limit_trips(db_session, email_adapter, monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_LOGIN_PER_EMAIL_MAX", "2")
    for _ in range(2):
        await start_email_login(
            db_session, email="ada@x.sg", ip="1.1.1.1", email_adapter=email_adapter
        )
    with pytest.raises(RateLimitExceeded) as exc_info:
        await start_email_login(
            db_session, email="ada@x.sg", ip="1.1.1.1", email_adapter=email_adapter
        )
    assert exc_info.value.scope == "login_start_per_email"
    assert exc_info.value.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_login_start_per_ip_limit_trips(db_session, email_adapter, monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_LOGIN_PER_IP_MAX", "2")
    monkeypatch.setenv("BSS_PORTAL_LOGIN_PER_EMAIL_MAX", "100")  # don't trip email cap first
    for i in range(2):
        await start_email_login(
            db_session,
            email=f"a{i}@x.sg",
            ip="2.2.2.2",
            email_adapter=email_adapter,
        )
    with pytest.raises(RateLimitExceeded) as exc_info:
        await start_email_login(
            db_session,
            email="a3@x.sg",
            ip="2.2.2.2",
            email_adapter=email_adapter,
        )
    assert exc_info.value.scope == "login_start_per_ip"


@pytest.mark.asyncio
async def test_verify_per_email_limit_trips(db_session, email_adapter, monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_VERIFY_PER_EMAIL_MAX", "3")
    await start_email_login(db_session, email="ada@x.sg", email_adapter=email_adapter)

    for _ in range(3):
        await verify_email_login(db_session, email="ada@x.sg", code="000000")

    with pytest.raises(RateLimitExceeded) as exc_info:
        await verify_email_login(db_session, email="ada@x.sg", code="000000")
    assert exc_info.value.scope == "login_verify_per_email"
