"""httpx tests for TMF676 paymentMethodManagement endpoints.

Every endpoint has at least one test with a camelCase JSON body (Phase 4 lesson).
"""

import pytest
from httpx import AsyncClient

PM_PATH = "/tmf-api/paymentMethodManagement/v4/paymentMethod"


def _valid_pm_body(customer_id: str = "CUST-001", token: str = "tok_test_1234") -> dict:
    return {
        "customerId": customer_id,
        "type": "card",
        "tokenizationProvider": "mock",
        "providerToken": token,
        "cardSummary": {
            "brand": "visa",
            "last4": "4242",
            "expMonth": 12,
            "expYear": 2030,
        },
    }


class TestCreatePaymentMethod:
    @pytest.mark.asyncio
    async def test_create_returns_201(self, client: AsyncClient):
        resp = await client.post(PM_PATH, json=_valid_pm_body())
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"].startswith("PM-")
        assert body["providerToken"] == "tok_test_1234"
        assert body["cardSummary"]["brand"] == "visa"
        assert body["cardSummary"]["last4"] == "4242"
        assert body["status"] == "active"
        assert body["isDefault"] is True
        assert body["@type"] == "PaymentMethod"
        # No PAN fields in response
        assert "cardNumber" not in body
        assert "cvv" not in body

    @pytest.mark.asyncio
    async def test_snake_case_aliases_also_accepted(self, client: AsyncClient):
        """populate_by_name=True accepts snake_case alongside the canonical
        camelCase (test_create_returns_201). This used to be an
        assertion-free test whose name claimed the opposite."""
        resp = await client.post(
            PM_PATH,
            json={
                "customer_id": "CUST-001",  # wrong: snake_case
                "type": "card",
                "tokenization_provider": "mock",
                "provider_token": "tok_abc",
                "card_summary": {"brand": "visa", "last4": "1234", "exp_month": 12, "exp_year": 2030},
            },
        )
        assert resp.status_code == 201, resp.text

    @pytest.mark.asyncio
    async def test_expired_card_rejected(self, client: AsyncClient):
        body = _valid_pm_body()
        body["cardSummary"]["expMonth"] = 1
        body["cardSummary"]["expYear"] = 2020
        resp = await client.post(PM_PATH, json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["reason"] == "payment_method.add.card_not_expired"

    @pytest.mark.asyncio
    async def test_unknown_customer_rejected(self, client: AsyncClient):
        """When CRM returns NotFound, the policy violation is raised."""
        from bss_clients import NotFound

        # Override the mock to raise NotFound
        client._transport.app.state.crm_client.get_customer.side_effect = NotFound(  # type: ignore[union-attr]
            "Customer CUST-999 not found"
        )
        body = _valid_pm_body(customer_id="CUST-999")
        resp = await client.post(PM_PATH, json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["reason"] == "payment_method.add.customer_exists"

        # Reset mock
        client._transport.app.state.crm_client.get_customer.side_effect = None  # type: ignore[union-attr]
        client._transport.app.state.crm_client.get_customer.return_value = {  # type: ignore[union-attr]
            "id": "CUST-001",
            "status": "active",
            "kycStatus": "verified",
        }

    @pytest.mark.asyncio
    async def test_closed_customer_rejected(self, client: AsyncClient):
        """Customer with status='closed' cannot add a payment method."""
        client._transport.app.state.crm_client.get_customer.return_value = {  # type: ignore[union-attr]
            "id": "CUST-002",
            "status": "closed",
            "kycStatus": "verified",
        }
        body = _valid_pm_body(customer_id="CUST-002")
        resp = await client.post(PM_PATH, json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["reason"] == "payment_method.add.customer_active_or_pending"

        # Reset mock
        client._transport.app.state.crm_client.get_customer.return_value = {  # type: ignore[union-attr]
            "id": "CUST-001",
            "status": "active",
            "kycStatus": "verified",
        }

    @pytest.mark.asyncio
    async def test_second_method_not_default(self, client: AsyncClient):
        """First method is default; second is not."""
        resp1 = await client.post(PM_PATH, json=_valid_pm_body(token="tok_first"))
        assert resp1.status_code == 201
        assert resp1.json()["isDefault"] is True

        resp2 = await client.post(PM_PATH, json=_valid_pm_body(token="tok_second"))
        assert resp2.status_code == 201
        assert resp2.json()["isDefault"] is False

    @pytest.mark.asyncio
    async def test_at_most_5_methods(self, client: AsyncClient):
        """Cannot exceed MAX_METHODS_PER_CUSTOMER (5)."""
        for i in range(5):
            resp = await client.post(PM_PATH, json=_valid_pm_body(token=f"tok_limit_{i}"))
            assert resp.status_code == 201

        resp = await client.post(PM_PATH, json=_valid_pm_body(token="tok_limit_overflow"))
        assert resp.status_code == 422
        assert resp.json()["reason"] == "payment_method.add.at_most_n_methods"


class TestGetPaymentMethod:
    @pytest.mark.asyncio
    async def test_get_by_id(self, client: AsyncClient):
        create_resp = await client.post(PM_PATH, json=_valid_pm_body(token="tok_get_test"))
        pm_id = create_resp.json()["id"]

        resp = await client.get(f"{PM_PATH}/{pm_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == pm_id
        assert body["customerId"] == "CUST-001"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_404(self, client: AsyncClient):
        resp = await client.get(f"{PM_PATH}/PM-9999")
        assert resp.status_code == 404


class TestListPaymentMethods:
    @pytest.mark.asyncio
    async def test_list_by_customer(self, client: AsyncClient):
        await client.post(PM_PATH, json=_valid_pm_body(token="tok_list_1"))
        await client.post(PM_PATH, json=_valid_pm_body(token="tok_list_2"))

        resp = await client.get(PM_PATH, params={"customerId": "CUST-001"})
        assert resp.status_code == 200
        methods = resp.json()
        assert len(methods) >= 2
        assert all(m["customerId"] == "CUST-001" for m in methods)


class TestRemovePaymentMethod:
    @pytest.mark.asyncio
    async def test_remove(self, client: AsyncClient):
        create_resp = await client.post(PM_PATH, json=_valid_pm_body(token="tok_remove_test"))
        pm_id = create_resp.json()["id"]

        resp = await client.delete(f"{PM_PATH}/{pm_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"

    @pytest.mark.asyncio
    async def test_remove_nonexistent_returns_422(self, client: AsyncClient):
        resp = await client.delete(f"{PM_PATH}/PM-9999")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_remove_already_removed_returns_422(self, client: AsyncClient):
        create_resp = await client.post(PM_PATH, json=_valid_pm_body(token="tok_double_remove"))
        pm_id = create_resp.json()["id"]

        await client.delete(f"{PM_PATH}/{pm_id}")
        resp = await client.delete(f"{PM_PATH}/{pm_id}")
        assert resp.status_code == 422


class TestHealth:
    @pytest.mark.asyncio
    async def test_health(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["service"] == "payment"

    @pytest.mark.asyncio
    async def test_ready(self, client: AsyncClient):
        resp = await client.get("/ready")
        assert resp.status_code == 200
