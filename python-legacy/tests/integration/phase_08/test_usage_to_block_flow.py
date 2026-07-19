"""Usage → exhaust → block, end-to-end through Mediation + Rating + Subscription."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from tests.integration.phase_08.conftest import (
    MEDIATION,
    get_balance,
    get_subscription,
    poll_until,
)

pytestmark = pytest.mark.integration


async def _post_usage(http: httpx.AsyncClient, msisdn: str, quantity_mb: int) -> httpx.Response:
    return await http.post(
        f"{MEDIATION}/tmf-api/usageManagement/v4/usage",
        json={
            "msisdn": msisdn,
            "eventType": "data",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "quantity": quantity_mb,
            "unit": "mb",
            "source": "integration-test",
        },
    )


@pytest.mark.asyncio
async def test_usage_consumes_balance(active_subscription):
    """Happy path: 1GB usage decrements bundle balance by 1024 MB."""
    sub_id = active_subscription["id"]
    msisdn = active_subscription["msisdn"]

    async with httpx.AsyncClient(timeout=10.0) as http:
        initial = await get_balance(http, sub_id, "data")
        assert initial is not None
        initial_remaining = initial["remaining"]

        resp = await _post_usage(http, msisdn, 1024)
        assert resp.status_code == 201, resp.text

        # Balance decrement happens via MQ — poll briefly for the change
        async def decremented():
            b = await get_balance(http, sub_id, "data")
            return b if b and b["remaining"] == initial_remaining - 1024 else None

        final = await poll_until(decremented, timeout_s=5.0)
        assert final is not None, "balance was not decremented after 5s"
        assert final["consumed"] == 1024


@pytest.mark.asyncio
async def test_usage_until_exhaust_blocks_subscription(active_subscription):
    """Consume full PLAN_M data allowance (30 GB) → subscription transitions to blocked."""
    sub_id = active_subscription["id"]
    msisdn = active_subscription["msisdn"]

    async with httpx.AsyncClient(timeout=15.0) as http:
        # PLAN_M = 30720 MB. Chunk into 6 × 5120 MB to exhaust.
        chunk_mb = 5120
        chunks = 6
        for i in range(chunks):
            resp = await _post_usage(http, msisdn, chunk_mb)
            # Early chunks must succeed; later chunks may be rejected once
            # the subscription has transitioned to blocked.
            assert resp.status_code in (201, 422), f"chunk {i}: {resp.status_code} {resp.text}"
            if resp.status_code == 422:
                # Once blocked, ingestion should be rejected with the right rule.
                body = resp.json()
                detail = body.get("detail") or body
                rule = (
                    detail.get("rule")
                    if isinstance(detail, dict)
                    else None
                )
                assert rule == "usage.record.subscription_must_be_active", body

        async def is_blocked():
            sub = await get_subscription(http, sub_id)
            return sub if sub and sub["state"] == "blocked" else None

        blocked = await poll_until(is_blocked, timeout_s=10.0)
        assert blocked is not None, "subscription did not transition to blocked"

        # After block, a fresh usage POST must be rejected at the edge
        reject = await _post_usage(http, msisdn, 100)
        assert reject.status_code == 422
        body = reject.json()
        detail = body.get("detail") or body
        rule = detail.get("rule") if isinstance(detail, dict) else None
        assert rule == "usage.record.subscription_must_be_active"
