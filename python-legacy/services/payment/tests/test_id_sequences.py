"""Test that ID generation survives app restart (Postgres sequences, not counters).

Creates two payment methods, simulates restart by creating a new app instance,
creates a third, verifies no PK collision and IDs are monotonically increasing.

NOTE: This test uses real commits (not rolled-back transactions) because it
needs to verify cross-restart behavior. It cleans up after itself.
"""

import pytest
from app.config import Settings
from app.main import create_app
from bss_middleware import TEST_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PM_PATH = "/tmf-api/paymentMethodManagement/v4/paymentMethod"

# Use a unique customer_id to avoid collisions with other tests
SEQ_TEST_CUSTOMER = "CUST-SEQ-TEST"


def _valid_pm_body(token: str) -> dict:
    return {
        "customerId": SEQ_TEST_CUSTOMER,
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


async def _make_client(settings: Settings):
    """Create a fresh app + client (simulates restart)."""
    from unittest.mock import AsyncMock

    from bss_clients import CRMClient

    app = create_app(settings)
    engine = create_async_engine(settings.db_url, pool_size=2, max_overflow=2)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    mock_crm = AsyncMock(spec=CRMClient)
    mock_crm.get_customer = AsyncMock(
        return_value={"id": SEQ_TEST_CUSTOMER, "status": "active"}
    )
    mock_crm.close = AsyncMock()
    app.state.crm_client = mock_crm

    transport = ASGITransport(app=app)
    client = AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-BSS-API-Token": TEST_TOKEN},
    )
    return client, engine


async def _cleanup(settings: Settings, created_ids: list[str]):
    """Delete test data created by the sequence test."""
    engine = create_async_engine(settings.db_url)
    async with engine.begin() as conn:
        for pm_id in created_ids:
            await conn.execute(
                text("DELETE FROM payment.payment_attempt WHERE payment_method_id = :id"),
                {"id": pm_id},
            )
            await conn.execute(
                text("DELETE FROM payment.payment_method WHERE id = :id"),
                {"id": pm_id},
            )
        # Also clean up any audit events
        await conn.execute(
            text("DELETE FROM audit.domain_event WHERE aggregate_id = ANY(:ids)"),
            {"ids": created_ids},
        )
    await engine.dispose()


class TestIdSequenceSurvivesRestart:
    @pytest.mark.asyncio
    async def test_no_pk_collision_after_restart(self):
        settings = Settings()
        if not settings.db_url:
            pytest.skip("BSS_DB_URL not set")

        created_ids: list[str] = []

        try:
            # --- App instance 1: create two methods ---
            client1, engine1 = await _make_client(settings)
            try:
                for i in range(2):
                    resp = await client1.post(
                        PM_PATH, json=_valid_pm_body(f"tok_seq_a_{i}")
                    )
                    assert resp.status_code == 201, resp.text
                    created_ids.append(resp.json()["id"])
            finally:
                await client1.aclose()
                await engine1.dispose()

            # --- App instance 2 (simulates restart): create a third ---
            client2, engine2 = await _make_client(settings)
            try:
                resp = await client2.post(
                    PM_PATH, json=_valid_pm_body("tok_seq_b_0")
                )
                assert resp.status_code == 201, resp.text
                created_ids.append(resp.json()["id"])
            finally:
                await client2.aclose()
                await engine2.dispose()

            # --- Verify ---
            assert len(created_ids) == 3
            assert len(set(created_ids)) == 3, f"Duplicate IDs: {created_ids}"

            # IDs are PM-NNNN — extract numeric parts and verify monotonic
            nums = [int(id_.split("-")[1]) for id_ in created_ids]
            assert nums == sorted(nums), f"IDs not monotonic: {created_ids}"
        finally:
            # Always clean up — this test uses real commits
            if created_ids:
                await _cleanup(settings, created_ids)
