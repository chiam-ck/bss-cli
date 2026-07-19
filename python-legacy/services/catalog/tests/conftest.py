"""Test fixtures for catalog service.

Tests assume `make seed` has been run against the target database.
The catalog service is read-only in v0.1 — no write isolation needed.
"""

import pytest
import pytest_asyncio
from bss_catalog.app import create_app
from bss_catalog.config import Settings
from bss_middleware import TEST_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


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


@pytest_asyncio.fixture
async def db_session(settings: Settings):
    engine = create_async_engine(settings.db_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(settings: Settings):
    app = create_app(settings)

    # httpx ASGITransport does not trigger ASGI lifespan events,
    # so we manually replicate what the lifespan context manager does.
    engine = create_async_engine(settings.db_url, pool_size=2, max_overflow=2)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-BSS-API-Token": TEST_TOKEN},
    ) as c:
        yield c

    await engine.dispose()
