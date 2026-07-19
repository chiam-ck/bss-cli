"""50-parallel decrement test — THE concurrency trap guard.

Two concurrent `usage.rated` events for the same (subscription, allowance)
can race: both read balance=100, both compute new_balance=50, last write
wins. Subscription guards against this with SELECT ... FOR UPDATE on the
balance row (see DECISIONS.md Phase 8 — SELECT FOR UPDATE over optimistic
and MQ-partitioning alternatives).

This test fires 50 parallel POST /usage calls for the same subscription and
asserts the final balance is deterministic: consumed == 50 * chunk_mb,
no lost decrements.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from tests.integration.phase_08.conftest import MEDIATION, get_balance, poll_until

pytestmark = pytest.mark.integration


async def _post_usage(http: httpx.AsyncClient, msisdn: str, quantity_mb: int) -> int:
    resp = await http.post(
        f"{MEDIATION}/tmf-api/usageManagement/v4/usage",
        json={
            "msisdn": msisdn,
            "eventType": "data",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "quantity": quantity_mb,
            "unit": "mb",
            "source": "concurrent-test",
        },
    )
    return resp.status_code


@pytest.mark.asyncio
async def test_50_parallel_usage_events_no_lost_decrements(active_subscription):
    """Fire 50 × 10MB usage events in parallel, assert consumed == 500MB exactly."""
    sub_id = active_subscription["id"]
    msisdn = active_subscription["msisdn"]

    n = 50
    chunk_mb = 10
    expected_consumed = n * chunk_mb  # 500MB — well under PLAN_M's 30720

    async with httpx.AsyncClient(timeout=15.0) as http:
        initial = await get_balance(http, sub_id, "data")
        assert initial is not None
        initial_consumed = initial["consumed"]

        # Fire all 50 POSTs concurrently
        results = await asyncio.gather(
            *(_post_usage(http, msisdn, chunk_mb) for _ in range(n)),
            return_exceptions=False,
        )

        # Every POST must have been accepted at the edge (subscription is
        # active, quantity valid, MSISDN belongs to sub).
        accepted = sum(1 for s in results if s == 201)
        assert accepted == n, (
            f"expected all {n} POSTs accepted; got {accepted}: statuses={results}"
        )

        # Wait for MQ fanout to settle — all 50 usage.rated events must
        # be consumed and their decrements applied.
        async def fully_settled():
            b = await get_balance(http, sub_id, "data")
            if not b:
                return None
            delta = b["consumed"] - initial_consumed
            return b if delta >= expected_consumed else None

        final = await poll_until(fully_settled, timeout_s=15.0, interval_s=0.2)
        assert final is not None, "balance never reached expected consumed after 15s"

        # No lost decrements AND no double-counting.
        delta = final["consumed"] - initial_consumed
        assert delta == expected_consumed, (
            f"expected consumed delta {expected_consumed} MB, got {delta} MB — "
            f"lost decrements or double-counting"
        )
