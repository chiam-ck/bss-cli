"""Test fixtures for Rating service."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.config import Settings
from app.logging import configure_logging
from app.main import create_app
from bss_clients import CatalogClient
from bss_middleware import TEST_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


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


PLAN_M_TARIFF = {
    "id": "PLAN_M",
    "name": "Standard",
    "productOfferingPrice": [
        {"priceType": "recurring", "price": {"taxIncludedAmount": {"value": "25.00", "unit": "SGD"}}},
    ],
    "bundleAllowance": [
        {"allowanceType": "data", "quantity": 30720, "unit": "mb"},
        {"allowanceType": "voice", "quantity": -1, "unit": "minutes"},
        {"allowanceType": "sms", "quantity": -1, "unit": "count"},
    ],
}


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
    conn = await db_engine.connect()
    txn = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)

    async def _fake_commit():
        await session.flush()

    session.commit = _fake_commit

    yield session

    await txn.rollback()
    await conn.close()


def _mock_catalog() -> AsyncMock:
    mock = AsyncMock(spec=CatalogClient)
    mock.get_offering = AsyncMock(return_value=PLAN_M_TARIFF)
    mock.close = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def client(settings: Settings, db_engine, db_session: AsyncSession):
    test_settings = Settings(db_url=settings.db_url)
    app = create_app(test_settings)
    app.state.engine = db_engine

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
    app.state.catalog_client = _mock_catalog()
    app.state.mq_exchange = None
    app.state.mq_connection = None

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-BSS-API-Token": TEST_TOKEN},
    ) as c:
        yield c


@pytest_asyncio.fixture
async def mock_clients(client):
    app = client._transport.app
    return {"catalog": app.state.catalog_client}
