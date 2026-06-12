"""v0.16 cutover endpoint tests.

POST /admin-api/v1/cutover/invalidate-mock-tokens marks every active
mock-token payment method as expired and emits a
payment_method.cutover_invalidated event per row. Operator runs this
BEFORE flipping BSS_PAYMENT_PROVIDER=mock → stripe so saved cards
fail immediately and the portal's "add a new card" flow recovers.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from bss_models import PaymentMethod
from bss_models.audit import DomainEvent
from sqlalchemy import select


@pytest_asyncio.fixture
async def seeded_methods(db_session):
    """Plant a mix of mock-token + stripe-token + already-expired rows."""
    rows = [
        PaymentMethod(
            id="PM-MOCK-001",
            customer_id="CUST-CUT-001",
            type="card",
            token="tok_mock_001",
            token_provider="mock",
            last4="4242",
            brand="visa",
            exp_month=12,
            exp_year=2030,
            is_default=True,
            status="active",
            tenant_id="DEFAULT",
        ),
        PaymentMethod(
            id="PM-MOCK-002",
            customer_id="CUST-CUT-002",
            type="card",
            token="tok_mock_002",
            token_provider="mock",
            last4="1234",
            brand="mastercard",
            exp_month=6,
            exp_year=2031,
            is_default=True,
            status="active",
            tenant_id="DEFAULT",
        ),
        PaymentMethod(
            id="PM-STRIPE-001",
            customer_id="CUST-CUT-003",
            type="card",
            token="pm_stripe_001",
            token_provider="stripe",
            last4="9999",
            brand="visa",
            exp_month=1,
            exp_year=2032,
            is_default=True,
            status="active",
            tenant_id="DEFAULT",
        ),
        PaymentMethod(
            id="PM-MOCK-EXPIRED",
            customer_id="CUST-CUT-004",
            type="card",
            token="tok_mock_already_expired",
            token_provider="mock",
            last4="0000",
            brand="visa",
            exp_month=12,
            exp_year=2030,
            is_default=False,
            status="expired",
            tenant_id="DEFAULT",
        ),
    ]
    for r in rows:
        db_session.add(r)
    await db_session.flush()
    return rows


_TEST_IDS = {"PM-MOCK-001", "PM-MOCK-002", "PM-STRIPE-001", "PM-MOCK-EXPIRED"}


class TestCutoverInvalidateMockTokens:
    """The endpoint operates over ALL active mock-token rows in the DB.

    Tests assert on the test's own seeded rows (scoped to _TEST_IDS) +
    deltas, never on absolute totals — the table may carry leftovers
    from prior soak/scenario runs that committed normally.
    """

    @pytest.mark.asyncio
    async def test_dry_run_returns_count_without_writing(
        self, client, seeded_methods, db_session
    ):
        resp = await client.post(
            "/admin-api/v1/cutover/invalidate-mock-tokens?dryRun=true"
        )
        assert resp.status_code == 200
        data = resp.json()
        # Both seeded mock-active rows are in the candidate list.
        ours = set(data["candidate_ids"]) & _TEST_IDS
        assert ours == {"PM-MOCK-001", "PM-MOCK-002"}
        assert data["invalidated_count"] == 0

        # Rows still active.
        await db_session.refresh(seeded_methods[0])
        await db_session.refresh(seeded_methods[1])
        assert seeded_methods[0].status == "active"
        assert seeded_methods[1].status == "active"

    @pytest.mark.asyncio
    async def test_real_run_invalidates_mock_rows_only(
        self, client, seeded_methods, db_session
    ):
        resp = await client.post(
            "/admin-api/v1/cutover/invalidate-mock-tokens"
        )
        assert resp.status_code == 200
        data = resp.json()
        # Our two mock-active rows are in the invalidated list.
        ours = set(data["invalidated_ids"]) & _TEST_IDS
        assert ours == {"PM-MOCK-001", "PM-MOCK-002"}

        # Active mock rows now expired.
        for pm in seeded_methods[:2]:
            await db_session.refresh(pm)
            assert pm.status == "expired"

        # Stripe row untouched.
        await db_session.refresh(seeded_methods[2])
        assert seeded_methods[2].status == "active"
        assert seeded_methods[2].token_provider == "stripe"

        # Already-expired mock row untouched (policy filters on
        # status='active', so an already-expired row was never a
        # candidate).
        await db_session.refresh(seeded_methods[3])
        assert seeded_methods[3].status == "expired"

    @pytest.mark.asyncio
    async def test_emits_one_event_per_invalidated_row(
        self, client, seeded_methods, db_session
    ):
        resp = await client.post(
            "/admin-api/v1/cutover/invalidate-mock-tokens"
        )
        assert resp.status_code == 200

        events = (
            await db_session.execute(
                select(DomainEvent).where(
                    DomainEvent.event_type == "payment_method.cutover_invalidated"
                )
            )
        ).scalars().all()
        # Filter to events for THIS test's seeded rows.
        ours = [
            e for e in events if e.aggregate_id in {"PM-MOCK-001", "PM-MOCK-002"}
        ]
        assert len(ours) == 2
        for e in ours:
            assert e.payload["reason"] == "operator_cutover"
            assert e.payload["customer_id"].startswith("CUST-CUT-")
            assert "last4" in e.payload
            assert "brand" in e.payload

    @pytest.mark.asyncio
    async def test_dry_run_does_not_emit_events(self, client, seeded_methods, db_session):
        # Baseline event count BEFORE the call.
        from sqlalchemy import func
        baseline_q = select(func.count(DomainEvent.event_id)).where(
            DomainEvent.event_type == "payment_method.cutover_invalidated"
        )
        baseline = (await db_session.execute(baseline_q)).scalar_one()

        resp = await client.post(
            "/admin-api/v1/cutover/invalidate-mock-tokens?dryRun=true"
        )
        assert resp.status_code == 200

        after = (await db_session.execute(baseline_q)).scalar_one()
        assert after == baseline, (
            "dry-run must NOT emit cutover_invalidated events"
        )
