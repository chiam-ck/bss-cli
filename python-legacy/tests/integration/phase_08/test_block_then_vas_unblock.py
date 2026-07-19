"""Blocked subscription → VAS purchase → unblocked → usage accepted again."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from tests.integration.phase_08.conftest import (
    MEDIATION,
    SUBSCRIPTION,
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
async def test_block_then_vas_unblock_then_usage(active_subscription):
    """Full loop: exhaust → block → VAS top-up → active → new usage accepted."""
    sub_id = active_subscription["id"]
    msisdn = active_subscription["msisdn"]

    async with httpx.AsyncClient(timeout=15.0) as http:
        # 1. Exhaust the bundle (30 GB in one shot).
        exhaust = await _post_usage(http, msisdn, 30720)
        assert exhaust.status_code == 201

        async def is_blocked():
            sub = await get_subscription(http, sub_id)
            return sub if sub and sub["state"] == "blocked" else None

        assert await poll_until(is_blocked, timeout_s=10.0), "did not block after full exhaust"

        # 2. VAS purchase — 5 GB top-up.
        vas_resp = await http.post(
            f"{SUBSCRIPTION}/subscription-api/v1/subscription/{sub_id}/vas-purchase",
            json={"vasOfferingId": "VAS_DATA_5GB"},
        )
        assert vas_resp.status_code == 200, vas_resp.text
        assert vas_resp.json()["state"] == "active"

        # 3. Balance should reflect the top-up.
        balance = await get_balance(http, sub_id, "data")
        assert balance is not None
        assert balance["remaining"] >= 5120, balance

        # 4. Fresh usage must be accepted now.
        resp = await _post_usage(http, msisdn, 512)
        assert resp.status_code == 201, resp.text

        async def decremented():
            b = await get_balance(http, sub_id, "data")
            return b if b and b["consumed"] >= 30720 + 512 else None

        assert await poll_until(decremented, timeout_s=5.0), "post-VAS usage did not decrement"
