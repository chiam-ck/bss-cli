"""Test fixtures for COM service."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.config import Settings
from app.logging import configure_logging
from app.main import create_app
from bss_clients import (
    CatalogClient,
    CRMClient,
    LoyaltyClient,
    PaymentClient,
    SOMClient,
    SubscriptionClient,
)
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


def _mock_crm() -> AsyncMock:
    mock = AsyncMock(spec=CRMClient)
    mock.get_customer = AsyncMock(return_value={
        "id": "CUST-0001",
        "status": "active",
        "kycStatus": "verified",
    })
    mock.close = AsyncMock()
    return mock


def _mock_catalog() -> AsyncMock:
    mock = AsyncMock(spec=CatalogClient)
    mock.get_offering = AsyncMock(return_value={
        "id": "PLAN_M",
        "name": "Standard",
        "productOfferingPrice": [{
            "id": "PRICE_PLAN_M",
            "priceType": "recurring",
            "price": {
                "taxIncludedAmount": {"value": "25.00", "unit": "SGD"},
            },
        }],
    })
    mock.get_active_price = AsyncMock(return_value={
        "id": "PRICE_PLAN_M",
        "priceType": "recurring",
        "price": {
            "taxIncludedAmount": {"value": "25.00", "unit": "SGD"},
        },
    })
    # v1.1 — default to "no promo" so create_order's discount discovery is a
    # no-op for the existing (non-promo) tests; promo tests override these.
    mock.validate_promo = AsyncMock(return_value={"valid": False, "reason": "unknown_code"})
    mock.resolve_eligible_promo = AsyncMock(
        return_value={"valid": False, "reason": "no_eligible_promo"}
    )
    mock.close = AsyncMock()
    return mock


def _mock_payment() -> AsyncMock:
    mock = AsyncMock(spec=PaymentClient)
    mock.list_methods = AsyncMock(return_value=[{
        "id": "PM-0001",
        "customerId": "CUST-0001",
        "isDefault": True,
        "status": "active",
    }])
    mock.close = AsyncMock()
    return mock


def _mock_som() -> AsyncMock:
    mock = AsyncMock(spec=SOMClient)
    # list_for_order returns empty (no service orders started)
    mock.list_for_order = AsyncMock(return_value=[])
    mock.close = AsyncMock()
    return mock


def _mock_subscription() -> AsyncMock:
    mock = AsyncMock(spec=SubscriptionClient)
    mock.create = AsyncMock(return_value={
        "id": "SUB-0001",
        "customerId": "CUST-0001",
        "state": "active",
    })
    mock.close = AsyncMock()
    return mock


def _mock_loyalty() -> AsyncMock:
    # v1.1 — COM's consume-lifecycle client. claim returns a synthetic offer id;
    # advance/redeem/revoke are no-ops. Non-promo tests never reach these.
    mock = AsyncMock(spec=LoyaltyClient)
    mock.claim_offer = AsyncMock(return_value={"offer_id": "OFF-CLAIMED-1", "state": "claimed"})
    mock.advance_offer_to_claimed = AsyncMock(return_value={"state": "claimed"})
    mock.redeem_offer = AsyncMock(return_value={"state": "redeemed"})
    mock.revoke_offer = AsyncMock(return_value={"state": "revoked"})
    mock.close = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def client(settings: Settings, db_engine, db_session: AsyncSession):
    app = create_app(settings)
    app.state.engine = db_engine
    app.state.mq_exchange = None
    app.state.mq_connection = None
    app.state.crm_client = _mock_crm()
    app.state.catalog_client = _mock_catalog()
    app.state.payment_client = _mock_payment()
    app.state.som_client = _mock_som()
    app.state.subscription_client = _mock_subscription()
    app.state.loyalty_client = _mock_loyalty()

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
    return {
        "crm": app.state.crm_client,
        "catalog": app.state.catalog_client,
        "payment": app.state.payment_client,
        "som": app.state.som_client,
        "subscription": app.state.subscription_client,
        "loyalty": app.state.loyalty_client,
    }
