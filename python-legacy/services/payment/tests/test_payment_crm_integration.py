"""Cross-service integration test: Payment → CRM (real container).

Requires CRM to be running: `docker compose up -d crm`

These tests exercise the real HTTP wire path. They catch docker networking,
service discovery, env variable, and container health issues.
"""

import socket
import uuid

import pytest
import pytest_asyncio
from app.config import Settings
from app.main import create_app
from bss_middleware import TEST_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = pytest.mark.integration

PM_PATH = "/tmf-api/paymentMethodManagement/v4/paymentMethod"


def _crm_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 8002), timeout=1)
        s.close()
        return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _skip_if_crm_down():
    if not _crm_reachable():
        pytest.skip("CRM service not running on localhost:8002")


@pytest_asyncio.fixture
async def integration_client():
    """Client wired to real CRM via localhost:8002."""
    s = Settings()
    if not s.db_url:
        pytest.skip("BSS_DB_URL not set")

    s.crm_url = "http://localhost:8002"

    app = create_app(s)

    engine = create_async_engine(s.db_url, pool_size=2, max_overflow=2)
    app.state.engine = engine

    conn = await engine.connect()
    txn = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)

    async def _fake_commit():
        await session.flush()

    session.commit = _fake_commit

    class _FakeSessionFactory:
        def __call__(self):
            return _FakeContextManager(session)

    class _FakeContextManager:
        def __init__(self, s):
            self._session = s

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, *args):
            pass

    app.state.session_factory = _FakeSessionFactory()

    # Manually create CRMClient (lifespan doesn't run in ASGI test transport)
    from bss_clients import CRMClient, NoAuthProvider

    crm_client = CRMClient(base_url=s.crm_url, auth_provider=NoAuthProvider())
    app.state.crm_client = crm_client

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-BSS-API-Token": TEST_TOKEN},
    ) as c:
        yield c

    await txn.rollback()
    await conn.close()
    if hasattr(app.state, "crm_client"):
        await app.state.crm_client.close()
    await engine.dispose()


class TestCRMIntegration:
    @pytest.mark.asyncio
    async def test_register_method_for_real_customer(self, integration_client):
        """Happy path: register a payment method for a customer that exists in CRM."""
        import httpx

        async with httpx.AsyncClient(base_url="http://localhost:8002") as crm:
            create_resp = await crm.post(
                "/tmf-api/customerManagement/v4/customer",
                json={
                    "givenName": "Integration",
                    "familyName": "Test",
                    "contactMedium": [
                        {
                            "mediumType": "email",
                            "value": f"integ-payment-{uuid.uuid4().hex[:8]}@test.com",
                            "isPrimary": True,
                        }
                    ],
                },
            )
            if create_resp.status_code != 201:
                pytest.skip(f"Could not create test customer: {create_resp.status_code}")
            customer_id = create_resp.json()["id"]

        resp = await integration_client.post(
            PM_PATH,
            json={
                "customerId": customer_id,
                "type": "card",
                "tokenizationProvider": "mock",
                "providerToken": "tok_integ_test_1234",
                "cardSummary": {
                    "brand": "visa",
                    "last4": "4242",
                    "expMonth": 12,
                    "expYear": 2030,
                },
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["customerId"] == customer_id
        assert body["status"] == "active"
