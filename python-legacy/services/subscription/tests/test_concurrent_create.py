"""Concurrent subscription creation — proves no PK collision or MSISDN/ICCID duplication.

Sequential creates through the test session verifying uniqueness constraints.
The real concurrency proof (SELECT FOR UPDATE SKIP LOCKED) lives in CRM's
inventory sub-domain; here we verify that the subscription service side
(Postgres sequence + UNIQUE constraints) holds under sequential load.
"""

import uuid

import pytest


@pytest.mark.asyncio
async def test_sequential_creates_no_collision(client, mock_clients):
    """10 sequential creates should produce 10 unique subscriptions."""
    sub_ids = []
    msisdns = set()
    iccids = set()

    # Per-run offset so MSISDN/ICCID UNIQUE constraints don't collide with
    # rows left by prior runs against the shared DB. Stay outside the
    # 90000xxx seed pool by living in the 91xxxxxx / 92xxxxxx range.
    run_offset = uuid.uuid4().int % 100000  # 5-digit offset, 0–99_999
    msisdn_base = 91_000_000 + run_offset * 10  # 8-digit MSISDN
    # Digits-only per-run prefix (ICCID is numeric); 6 digits from uuid keeps
    # total length at 16 to match the original test shape.
    iccid_prefix = f"891010{uuid.uuid4().int % 1_000_000:06d}"

    for i in range(10):
        resp = await client.post(
            "/subscription-api/v1/subscription",
            json={
                "customerId": f"CUST-{i:04d}",
                "offeringId": "PLAN_M",
                "msisdn": str(msisdn_base + i),
                "iccid": f"{iccid_prefix}{i:04d}",
                "paymentMethodId": f"PM-{i:04d}",
            },
        )
        assert resp.status_code == 201, f"Create #{i} failed: {resp.text}"
        body = resp.json()
        sub_ids.append(body["id"])
        msisdns.add(body["msisdn"])
        iccids.add(body["iccid"])

    assert len(set(sub_ids)) == 10, f"PK collision: {sub_ids}"
    assert len(msisdns) == 10, f"MSISDN duplication: {msisdns}"
    assert len(iccids) == 10, f"ICCID duplication: {iccids}"

    # Verify IDs are monotonic
    nums = [int(s.split("-")[1]) for s in sub_ids]
    for a, b in zip(nums, nums[1:]):
        assert a < b, f"IDs not monotonic: {sub_ids}"
