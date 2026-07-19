"""Cross-service failure tests: Payment → CRM (respx mocked).

Simulates CRM returning 404, 503, malformed body, and timeout.
Also tests actor chain propagation across service boundaries.
"""

import pytest
import pytest_asyncio
import respx
from app.config import Settings
from app.main import create_app
from bss_middleware import TEST_TOKEN
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

PM_PATH = "/tmf-api/paymentMethodManagement/v4/paymentMethod"
CRM_CUSTOMER_URL = "http://crm-test:8000/tmf-api/customerManagement/v4/customer"


@pytest_asyncio.fixture
async def respx_client():
    """ASGI client with real CRMClient pointed at a respx-mocked URL."""
    from bss_clients import CRMClient, NoAuthProvider

    s = Settings()
    if not s.db_url:
        pytest.fail("BSS_DB_URL is not set")
    s.crm_url = "http://crm-test:8000"

    app = create_app(s)

    engine = create_async_engine(s.db_url, pool_size=2, max_overflow=2)
    app.state.engine = engine

    # Manually create CRMClient (lifespan doesn't run in ASGI test transport)
    crm_client = CRMClient(base_url=s.crm_url, auth_provider=NoAuthProvider())
    app.state.crm_client = crm_client

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

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-BSS-API-Token": TEST_TOKEN},
    ) as c:
        yield c

    await txn.rollback()
    await conn.close()
    await crm_client.close()
    await engine.dispose()


def _valid_pm_body(customer_id: str = "CUST-001") -> dict:
    return {
        "customerId": customer_id,
        "type": "card",
        "tokenizationProvider": "mock",
        "providerToken": "tok_respx_test",
        "cardSummary": {
            "brand": "visa",
            "last4": "4242",
            "expMonth": 12,
            "expYear": 2030,
        },
    }


class TestCRMNotFound:
    @pytest.mark.asyncio
    @respx.mock
    async def test_unknown_customer_returns_policy_violation(self, respx_client):
        """CRM returns 404 → payment_method.add.customer_exists violation."""
        respx.get(f"{CRM_CUSTOMER_URL}/CUST-999").mock(
            return_value=Response(404, text="Customer CUST-999 not found")
        )
        resp = await respx_client.post(PM_PATH, json=_valid_pm_body("CUST-999"))
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "POLICY_VIOLATION"
        assert body["reason"] == "payment_method.add.customer_exists"


class TestCRMServerError:
    @pytest.mark.asyncio
    @respx.mock
    async def test_crm_503_propagates(self, respx_client):
        """CRM returns 503 → ServerError propagated (not swallowed)."""
        respx.get(f"{CRM_CUSTOMER_URL}/CUST-001").mock(
            return_value=Response(503, text="Service unavailable")
        )
        resp = await respx_client.post(PM_PATH, json=_valid_pm_body())
        # ServerError from bss-clients is not a PolicyViolation,
        # so it should surface as a 500 from FastAPI's default handler
        assert resp.status_code == 500


class TestCRMMalformedBody:
    @pytest.mark.asyncio
    @respx.mock
    async def test_crm_malformed_response(self, respx_client):
        """CRM returns 200 but malformed body — customer dict missing 'status'."""
        respx.get(f"{CRM_CUSTOMER_URL}/CUST-001").mock(
            return_value=Response(200, json={"id": "CUST-001"})
            # Missing "status" key
        )
        resp = await respx_client.post(PM_PATH, json=_valid_pm_body())
        # check_customer_active_or_pending gets status="" from .get("status", "")
        # which is not in ("active", "pending"), so it raises a PolicyViolation
        assert resp.status_code == 422
        assert resp.json()["reason"] == "payment_method.add.customer_active_or_pending"


class TestActorChainPropagation:
    @pytest.mark.asyncio
    @respx.mock
    async def test_actor_chain_propagated_across_service_boundary(self, respx_client):
        """Inbound X-BSS-Actor/Channel headers survive the hop to CRM.

        This proves the full chain:
        inbound HTTP → middleware → auth_context → bss-clients → outbound HTTP.

        If any link breaks, CRM would see actor=system instead of actor=alice,
        and Phase 4's interaction auto-logging would attribute the action to
        the wrong principal.
        """
        route = respx.get(f"{CRM_CUSTOMER_URL}/CUST-001").mock(
            return_value=Response(200, json={"id": "CUST-001", "status": "active"})
        )

        await respx_client.post(
            PM_PATH,
            json=_valid_pm_body(),
            headers={
                "X-BSS-Actor": "alice",
                "X-BSS-Channel": "cli",
            },
        )

        assert route.called
        outgoing_request = route.calls[0].request
        assert outgoing_request.headers["x-bss-actor"] == "alice"
        assert outgoing_request.headers["x-bss-channel"] == "cli"
        # Request ID should be propagated (non-empty)
        assert len(outgoing_request.headers.get("x-request-id", "")) > 0
