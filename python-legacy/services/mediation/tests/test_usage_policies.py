"""Direct unit tests for mediation policy functions."""

from unittest.mock import AsyncMock

import pytest
from app.policies.base import PolicyViolation
from app.policies.usage import (
    check_msisdn_matches,
    check_positive_quantity,
    check_subscription_active,
    check_subscription_exists,
    check_valid_event_type,
)
from bss_clients import SubscriptionClient
from bss_clients.errors import NotFound


def test_positive_quantity_passes():
    check_positive_quantity(1)
    check_positive_quantity(1_000_000)


@pytest.mark.parametrize("q", [0, -1, -1_000_000])
def test_positive_quantity_rejects(q):
    with pytest.raises(PolicyViolation) as exc:
        check_positive_quantity(q)
    assert exc.value.rule == "usage.record.positive_quantity"
    assert exc.value.context["quantity"] == q


@pytest.mark.parametrize("t", ["data", "voice", "voice_minutes", "sms"])
def test_valid_event_type_passes(t):
    check_valid_event_type(t)


@pytest.mark.parametrize("t", ["video", "", "DATA", "unknown"])
def test_valid_event_type_rejects(t):
    with pytest.raises(PolicyViolation) as exc:
        check_valid_event_type(t)
    assert exc.value.rule == "usage.record.valid_event_type"


@pytest.mark.asyncio
async def test_subscription_must_exist_passes():
    client = AsyncMock(spec=SubscriptionClient)
    client.get_by_msisdn = AsyncMock(return_value={"id": "SUB-0001", "msisdn": "90000042"})
    sub = await check_subscription_exists("90000042", client)
    assert sub["id"] == "SUB-0001"


@pytest.mark.asyncio
async def test_subscription_must_exist_rejects_on_notfound():
    client = AsyncMock(spec=SubscriptionClient)
    client.get_by_msisdn = AsyncMock(side_effect=NotFound("no sub"))
    with pytest.raises(PolicyViolation) as exc:
        await check_subscription_exists("90000042", client)
    assert exc.value.rule == "usage.record.subscription_must_exist"


def test_msisdn_matches_passes():
    check_msisdn_matches({"id": "SUB-0001", "msisdn": "90000042"}, "90000042")


def test_msisdn_matches_rejects_mismatch():
    with pytest.raises(PolicyViolation) as exc:
        check_msisdn_matches({"id": "SUB-0001", "msisdn": "90000043"}, "90000042")
    assert exc.value.rule == "usage.record.msisdn_belongs_to_subscription"


def test_subscription_active_passes():
    check_subscription_active({"id": "SUB-0001", "state": "active"})


@pytest.mark.parametrize("state", ["blocked", "terminated", "suspended", "pending"])
def test_subscription_active_rejects_non_active(state):
    with pytest.raises(PolicyViolation) as exc:
        check_subscription_active({"id": "SUB-0001", "state": state})
    assert exc.value.rule == "usage.record.subscription_must_be_active"
    assert exc.value.context["state"] == state
