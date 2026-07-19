"""Shared fixtures for bss-cockpit tests.

Tests assume ``make migrate`` has applied 0014. Each test gets its own
``ConversationStore`` bound to the dev DB and truncates the cockpit
schema before+after the case so runs are independent.

Mirrors packages/bss-portal-auth/tests/conftest.py.
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
    BSS_DB_URL: str = ""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


from bss_clock.clock import reset_for_tests as _reset_clock  # noqa: E402
from bss_cockpit import ConversationStore, configure_store  # noqa: E402


@pytest.fixture(autouse=True)
def _clock_reset():
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
async def store(db_url: str):
    """Per-test ConversationStore. Truncates cockpit.* before+after."""
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await _truncate(session)
        await session.commit()

    s = ConversationStore(engine=engine)
    configure_store(s)
    try:
        yield s
    finally:
        configure_store(None)

    async with factory() as session:
        await _truncate(session)
        await session.commit()
    await engine.dispose()


async def _truncate(session) -> None:
    # CASCADE so pending_destructive + message + session all clear regardless
    # of FK shape.
    await session.execute(
        text(
            "TRUNCATE cockpit.pending_destructive, cockpit.message, "
            "cockpit.session RESTART IDENTITY CASCADE"
        )
    )
