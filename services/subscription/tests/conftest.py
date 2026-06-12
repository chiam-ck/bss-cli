"""Test fixtures for Subscription service.

Write isolation: every test runs inside a transaction that is rolled back
in teardown. session.commit() is monkeypatched to flush() so writes are
visible within the test but the outer transaction is never committed.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.config import Settings
from app.logging import configure_logging
from app.main import create_app
from bss_clients import CatalogClient, CRMClient, InventoryClient, PaymentClient
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
    """Per-test session wrapped in a transaction that is always rolled back."""
    conn = await db_engine.connect()
    txn = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)

    _original_commit = session.commit

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


def _mock_payment() -> AsyncMock:
    mock = AsyncMock(spec=PaymentClient)
    mock.charge = AsyncMock(return_value={
        "id": "PAY-000001",
        "status": "approved",
        "gatewayRef": "mock_ref",
    })
    mock.list_methods = AsyncMock(return_value=[{
        "id": "PM-0001",
        "customerId": "CUST-0001",
        "isDefault": True,
        "status": "active",
    }])
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
        "bundleAllowance": [
            {"allowanceType": "data", "quantity": 30720, "unit": "mb"},
            {"allowanceType": "voice", "quantity": -1, "unit": "minutes"},
            {"allowanceType": "sms", "quantity": -1, "unit": "count"},
        ],
    })
    mock.list_active_offerings = AsyncMock(return_value=[
        {"id": "PLAN_S", "name": "Lite"},
        {"id": "PLAN_M", "name": "Standard"},
        {"id": "PLAN_L", "name": "Max"},
    ])
    mock.get_active_price = AsyncMock(return_value={
        "id": "PRICE_PLAN_L",
        "priceType": "recurring",
        "price": {"taxIncludedAmount": {"value": "45.00", "unit": "SGD"}},
    })
    mock.get_offering_price = AsyncMock(return_value={
        "id": "PRICE_PLAN_L",
        "priceType": "recurring",
        "price": {"taxIncludedAmount": {"value": "45.00", "unit": "SGD"}},
    })
    mock.get_vas = AsyncMock(return_value={
        "id": "VAS_DATA_1GB",
        "name": "Data Top-Up 1GB",
        "priceAmount": "3.00",
        "currency": "SGD",
        "allowanceType": "data",
        "allowanceQuantity": 1024,
        "allowanceUnit": "mb",
        "expiryHours": None,
    })
    mock.close = AsyncMock()
    return mock


def _mock_inventory() -> AsyncMock:
    mock = AsyncMock(spec=InventoryClient)
    mock.get_msisdn = AsyncMock(return_value={
        "msisdn": "90000042",
        "status": "reserved",
    })
    mock.get_esim = AsyncMock(return_value={
        "iccid": "8910000000000042",
        "profileState": "reserved",
    })
    mock.assign_msisdn = AsyncMock(return_value={"msisdn": "90000042", "status": "assigned"})
    mock.assign_msisdn_to_esim = AsyncMock(return_value={"iccid": "8910000000000042"})
    mock.release_msisdn = AsyncMock(return_value={"msisdn": "90000042", "status": "available"})
    mock.recycle_esim = AsyncMock(return_value={"iccid": "8910000000000042", "profileState": "recycled"})
    mock.close = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def client(settings: Settings, db_engine, db_session: AsyncSession):
    """ASGI test client wired to the rolled-back session."""
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
    app.state.crm_client = _mock_crm()
    app.state.payment_client = _mock_payment()
    app.state.catalog_client = _mock_catalog()
    app.state.inventory_client = _mock_inventory()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-BSS-API-Token": TEST_TOKEN},
    ) as c:
        yield c


@pytest_asyncio.fixture
async def mock_clients(client):
    """Return the mock clients for assertion in tests."""
    app = client._transport.app
    return {
        "crm": app.state.crm_client,
        "payment": app.state.payment_client,
        "catalog": app.state.catalog_client,
        "inventory": app.state.inventory_client,
    }


@pytest_asyncio.fixture
async def simulate_usage(client, db_session):
    """Invoke SubscriptionService.handle_usage_rated directly.

    Replaces the Phase 6 `consume-for-test` HTTP endpoint for unit tests
    that want to simulate the Rating → Subscription event without wiring
    RabbitMQ. The real consumer is exercised in integration tests.
    """
    from app.repositories.subscription_repo import SubscriptionRepository
    from app.repositories.vas_repo import VasPurchaseRepository
    from app.services.subscription_service import SubscriptionService

    app = client._transport.app

    async def _call(
        sub_id: str,
        allowance_type: str,
        quantity: int,
        usage_event_id: str = "UE-TEST-0001",
    ) -> None:
        svc = SubscriptionService(
            session=db_session,
            repo=SubscriptionRepository(db_session),
            vas_repo=VasPurchaseRepository(db_session),
            crm_client=app.state.crm_client,
            payment_client=app.state.payment_client,
            catalog_client=app.state.catalog_client,
            inventory_client=app.state.inventory_client,
        )
        await svc.handle_usage_rated(
            subscription_id=sub_id,
            allowance_type=allowance_type,
            consumed_quantity=quantity,
            usage_event_id=usage_event_id,
        )

    return _call
