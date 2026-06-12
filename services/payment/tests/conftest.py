"""Test fixtures for Payment service.

Write isolation: every test runs inside a transaction that is rolled back
in teardown. session.commit() is monkeypatched to flush() so writes are
visible within the test but the outer transaction is never committed.
"""

import pytest
import pytest_asyncio
from app.config import Settings
from app.logging import configure_logging
from app.main import create_app
from bss_middleware import TEST_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)


@pytest.fixture(scope="session", autouse=True)
def _bss_api_token_env():
    """v0.3 — middleware reads BSS_API_TOKEN at app construction; set before."""
    import os
    prev = os.environ.get("BSS_API_TOKEN")
    os.environ["BSS_API_TOKEN"] = TEST_TOKEN
    yield
    if prev is None:
        os.environ.pop("BSS_API_TOKEN", None)
    else:
        os.environ["BSS_API_TOKEN"] = prev


@pytest.fixture(scope="session")
def settings(_bss_api_token_env) -> Settings:
    s = Settings()
    if not s.db_url:
        pytest.fail("BSS_DB_URL is not set. Export it or add to .env.")
    return s


@pytest.fixture(scope="session", autouse=True)
def _configure_logging(settings: Settings) -> None:
    configure_logging(settings.log_level)


@pytest_asyncio.fixture
async def db_engine(settings: Settings):
    engine = create_async_engine(settings.db_url, pool_size=2, max_overflow=2)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Per-test session wrapped in a transaction that is always rolled back.

    session.commit() is replaced with flush() so the service layer's commits
    don't escape the test transaction.
    """
    conn = await db_engine.connect()
    txn = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)

    # Monkeypatch: commit → flush (writes visible, but outer txn stays open)
    _original_commit = session.commit

    async def _fake_commit():
        await session.flush()

    session.commit = _fake_commit

    yield session

    await txn.rollback()
    await conn.close()


@pytest_asyncio.fixture
async def client(settings: Settings, db_engine, db_session: AsyncSession):
    """ASGI test client wired to the rolled-back session."""
    from unittest.mock import AsyncMock

    from bss_clients import CRMClient

    app = create_app(settings)

    # Store engine on app.state for /ready endpoint
    app.state.engine = db_engine

    # Override the session factory so every request reuses the test session
    class _FakeSessionFactory:
        def __call__(self):
            return _FakeContextManager(db_session)

    class _FakeContextManager:
        def __init__(self, session):
            self._session = session

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, *args):
            pass

    app.state.session_factory = _FakeSessionFactory()

    # v0.16: tests bypass lifespan, so the tokenizer adapter must be
    # hung off app.state by hand. MockTokenizerAdapter preserves every
    # FAIL/DECLINE test affordance the existing payment tests rely on.
    from app.domain.mock_tokenizer import MockTokenizerAdapter
    app.state.tokenizer = MockTokenizerAdapter()

    # Mock CRM client — returns active customer by default
    mock_crm = AsyncMock(spec=CRMClient)
    mock_crm.get_customer = AsyncMock(
        return_value={
            "id": "CUST-001",
            "status": "active",
            "kycStatus": "verified",
        }
    )
    mock_crm.close = AsyncMock()
    app.state.crm_client = mock_crm

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-BSS-API-Token": TEST_TOKEN},
    ) as c:
        yield c
