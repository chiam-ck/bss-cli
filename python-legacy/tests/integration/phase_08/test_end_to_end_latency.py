"""End-to-end latency: POST /usage → subscription balance update.

Doctrine #6: lightweight is measurable. Phase 8 target is p99 < 500ms locally
for the full usage → rated → decrement chain. This runs 20 sequential
usage events and records the time from POST /usage returning to the
subscription balance reflecting the decrement.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest

from tests.integration.phase_08.conftest import MEDIATION, get_balance

pytestmark = pytest.mark.integration


async def _usage_roundtrip_ms(
    http: httpx.AsyncClient, sub_id: str, msisdn: str, expected_consumed: int
) -> float:
    t0 = time.perf_counter()
    resp = await http.post(
        f"{MEDIATION}/tmf-api/usageManagement/v4/usage",
        json={
            "msisdn": msisdn,
            "eventType": "data",
            "eventTime": datetime.now(timezone.utc).isoformat(),
            "quantity": 10,
            "unit": "mb",
            "source": "latency-test",
        },
    )
    assert resp.status_code == 201, resp.text

    # Poll for balance decrement
    deadline = t0 + 2.0  # 2s hard ceiling per event
    while time.perf_counter() < deadline:
        b = await get_balance(http, sub_id, "data")
        if b and b["consumed"] >= expected_consumed:
            return (time.perf_counter() - t0) * 1000.0
        await asyncio.sleep(0.005)

    raise AssertionError(
        f"balance did not reflect decrement within 2s (expected consumed>={expected_consumed})"
    )


@pytest.mark.asyncio
async def test_end_to_end_latency_p99_under_500ms(active_subscription):
    sub_id = active_subscription["id"]
    msisdn = active_subscription["msisdn"]

    samples: list[float] = []
    async with httpx.AsyncClient(timeout=10.0) as http:
        # Warm up
        await _usage_roundtrip_ms(http, sub_id, msisdn, expected_consumed=10)

        consumed = 10
        for _ in range(20):
            consumed += 10
            elapsed = await _usage_roundtrip_ms(http, sub_id, msisdn, expected_consumed=consumed)
            samples.append(elapsed)

    samples.sort()
    p50 = samples[len(samples) // 2]
    p99_idx = max(0, int(len(samples) * 0.99) - 1)
    p99 = samples[p99_idx]

    print(f"\nlatency samples (ms): p50={p50:.1f} p99={p99:.1f} n={len(samples)}")
    assert p99 < 500.0, f"p99 latency {p99:.1f}ms exceeds 500ms target"
