"""httpx tests for TMF676 paymentManagement endpoints.

Tests charge happy path, decline path, and all policy violations.
All requests use camelCase JSON bodies.
"""

import pytest
from httpx import AsyncClient

PM_PATH = "/tmf-api/paymentMethodManagement/v4/paymentMethod"
PAY_PATH = "/tmf-api/paymentManagement/v4/payment"


def _valid_pm_body(token: str = "tok_charge_test") -> dict:
    return {
        "customerId": "CUST-001",
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


async def _create_pm(client: AsyncClient, token: str = "tok_charge_test") -> str:
    resp = await client.post(PM_PATH, json=_valid_pm_body(token=token))
    assert resp.status_code == 201
    return resp.json()["id"]


class TestCharge:
    @pytest.mark.asyncio
    async def test_charge_approved(self, client: AsyncClient):
        pm_id = await _create_pm(client, token="tok_approved_test")
        resp = await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-001",
                "paymentMethodId": pm_id,
                "amount": "10.00",
                "currency": "SGD",
                "purpose": "activation",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"].startswith("PAY-")
        assert body["status"] == "approved"
        assert body["gatewayRef"] is not None
        assert body["gatewayRef"].startswith("mock_")
        assert body["declineReason"] is None
        assert body["@type"] == "Payment"

    @pytest.mark.asyncio
    async def test_charge_declined(self, client: AsyncClient):
        """Token with FAIL in it → declined (HTTP 201, not 4xx)."""
        pm_id = await _create_pm(client, token="tok_FAIL_decline")
        resp = await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-001",
                "paymentMethodId": pm_id,
                "amount": "10.00",
                "currency": "SGD",
                "purpose": "activation",
            },
        )
        # Declines are business outcomes, not errors
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "declined"
        assert body["declineReason"] == "card_declined_by_issuer"

    @pytest.mark.asyncio
    async def test_charge_persists_idempotency_key_on_attempt_row(
        self, client: AsyncClient, db_session
    ):
        """v0.16: every charge stamps ATT-{id}-r0 on payment_attempt.idempotency_key.

        Foundation for the v1.0 crash-recovery path. Forensics today;
        crash-restart-detect tomorrow. See docs/runbooks/payment-idempotency.md.
        """
        from bss_models import PaymentAttempt
        from sqlalchemy import select

        pm_id = await _create_pm(client, token="tok_idempotency_test")
        resp = await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-001",
                "paymentMethodId": pm_id,
                "amount": "10.00",
                "currency": "SGD",
                "purpose": "activation",
            },
        )
        assert resp.status_code == 201
        attempt_id = resp.json()["id"]

        row = (
            await db_session.execute(
                select(PaymentAttempt).where(PaymentAttempt.id == attempt_id)
            )
        ).scalar_one()
        assert row.idempotency_key == f"ATT-{attempt_id}-r0"

    @pytest.mark.asyncio
    async def test_charge_negative_amount_rejected(self, client: AsyncClient):
        pm_id = await _create_pm(client, token="tok_neg_test")
        resp = await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-001",
                "paymentMethodId": pm_id,
                "amount": "-5.00",
                "currency": "SGD",
                "purpose": "activation",
            },
        )
        assert resp.status_code == 422
        assert resp.json()["reason"] == "payment.charge.positive_amount"

    @pytest.mark.asyncio
    async def test_charge_removed_method_rejected(self, client: AsyncClient):
        pm_id = await _create_pm(client, token="tok_removed_charge")
        await client.delete(f"{PM_PATH}/{pm_id}")

        resp = await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-001",
                "paymentMethodId": pm_id,
                "amount": "10.00",
                "currency": "SGD",
                "purpose": "activation",
            },
        )
        assert resp.status_code == 422
        assert resp.json()["reason"] == "payment.charge.method_active"

    @pytest.mark.asyncio
    async def test_charge_wrong_customer_rejected(self, client: AsyncClient):
        pm_id = await _create_pm(client, token="tok_wrong_cust")
        resp = await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-999",
                "paymentMethodId": pm_id,
                "amount": "10.00",
                "currency": "SGD",
                "purpose": "activation",
            },
        )
        assert resp.status_code == 422
        assert resp.json()["reason"] == "payment.charge.customer_matches_method"

    @pytest.mark.asyncio
    async def test_charge_nonexistent_method_rejected(self, client: AsyncClient):
        resp = await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-001",
                "paymentMethodId": "PM-9999",
                "amount": "10.00",
                "currency": "SGD",
                "purpose": "activation",
            },
        )
        assert resp.status_code == 422


class TestGetPayment:
    @pytest.mark.asyncio
    async def test_get_by_id(self, client: AsyncClient):
        pm_id = await _create_pm(client, token="tok_get_pay")
        charge_resp = await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-001",
                "paymentMethodId": pm_id,
                "amount": "25.00",
                "currency": "SGD",
                "purpose": "renewal",
            },
        )
        pay_id = charge_resp.json()["id"]

        resp = await client.get(f"{PAY_PATH}/{pay_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == pay_id
        assert body["amount"] == "25.00"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_404(self, client: AsyncClient):
        resp = await client.get(f"{PAY_PATH}/PAY-999999")
        assert resp.status_code == 404


class TestListPayments:
    @pytest.mark.asyncio
    async def test_list_by_customer(self, client: AsyncClient):
        pm_id = await _create_pm(client, token="tok_list_pay")
        await client.post(
            PAY_PATH,
            json={
                "customerId": "CUST-001",
                "paymentMethodId": pm_id,
                "amount": "10.00",
                "currency": "SGD",
                "purpose": "activation",
            },
        )

        resp = await client.get(PAY_PATH, params={"customerId": "CUST-001"})
        assert resp.status_code == 200
        assert len(resp.json()) >= 1
