"""Shared fixtures for bss-portal-auth tests.

Tests assume `make migrate` has applied 0008. Each test gets its own
DB session and a clean ``portal_auth`` schema (truncated between
cases). The pepper is set to a fixed test value so HMAC outputs are
stable; the bss-clock is reset between cases so frozen-clock scenarios
don't leak into the next test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[3]


class _DbSettings(BaseSettings):
    """Minimal pydantic-settings to read BSS_DB_URL from .env / env vars.

    Mirrors the per-service pattern (services/catalog/bss_catalog/config.py).
    Lets `make test` work without callers sourcing `.env` first.
    """

    BSS_DB_URL: str = ""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

# 64-char hex test pepper. Must be set BEFORE any module that reads
# Settings() lazily — so we set it at import time, not via a fixture.
os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
# v0.14: force-clear the public-URL setting so login-flow tests get the
# bare-token magic-link path (matches the test's verify-by-magic-link
# expectations). A developer-sourced .env with BSS_PORTAL_PUBLIC_URL set
# would otherwise leak into start_email_login → magic_link becomes a
# full URL → tests trying to use it as ``code`` fail.
os.environ["BSS_PORTAL_PUBLIC_URL"] = ""

from bss_clock.clock import reset_for_tests as _reset_clock  # noqa: E402
from bss_portal_auth import NoopEmailAdapter  # noqa: E402


@pytest.fixture(autouse=True)
def _clock_reset():
    """Every test starts with a fresh wall-clock state."""
    _reset_clock()
    yield
    _reset_clock()


@pytest.fixture
def db_url() -> str:
    url = _DbSettings().BSS_DB_URL or os.environ.get("BSS_DB_URL", "")
    if not url:
        pytest.fail("BSS_DB_URL is not set. Export it or add to .env.")
    return url


@pytest_asyncio.fixture
async def db_session(db_url: str):
    """Per-test session that scrubs portal_auth before AND after."""
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await _truncate(session)
        await session.commit()

    async with factory() as session:
        yield session
        # Tests sometimes flush but don't commit — make sure we don't leak.
        await session.rollback()

    async with factory() as session:
        await _truncate(session)
        await session.commit()
    await engine.dispose()


async def _truncate(session) -> None:
    # Order matters: child tables before parent identity. Use TRUNCATE
    # CASCADE to keep this resilient if a future migration adds FKs.
    await session.execute(text(
        "TRUNCATE portal_auth.login_attempt, portal_auth.session, "
        "portal_auth.login_token, portal_auth.identity RESTART IDENTITY CASCADE"
    ))


@pytest.fixture
def email_adapter() -> NoopEmailAdapter:
    return NoopEmailAdapter()
