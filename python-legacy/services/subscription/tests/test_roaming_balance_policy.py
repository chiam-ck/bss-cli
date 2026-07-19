"""v0.17 — `subscription.usage_rated.roaming_balance_required` policy.

Pure unit test; no DB. Covers the doctrine that
``data_roaming`` exhaustion blocks the *roaming* decrement only — not
the subscription itself.
"""

from dataclasses import dataclass

import pytest
from app.policies.base import PolicyViolation
from app.policies.usage import check_roaming_balance_required


@dataclass
class _FakeBalance:
    total: int
    consumed: int


class TestRoamingBalancePolicy:
    def test_missing_row_rejects(self):
        with pytest.raises(PolicyViolation) as exc:
            check_roaming_balance_required(
                subscription_id="SUB-001",
                balance=None,
                consumed_quantity=10,
            )
        assert (
            exc.value.rule
            == "subscription.usage_rated.roaming_balance_required"
        )

    def test_exhausted_row_rejects(self):
        with pytest.raises(PolicyViolation) as exc:
            check_roaming_balance_required(
                subscription_id="SUB-001",
                balance=_FakeBalance(total=500, consumed=500),
                consumed_quantity=10,
            )
        assert (
            exc.value.rule
            == "subscription.usage_rated.roaming_balance_required"
        )

    def test_overconsumed_row_rejects(self):
        with pytest.raises(PolicyViolation):
            check_roaming_balance_required(
                subscription_id="SUB-001",
                balance=_FakeBalance(total=500, consumed=600),
                consumed_quantity=10,
            )

    def test_partial_balance_allowed(self):
        check_roaming_balance_required(
            subscription_id="SUB-001",
            balance=_FakeBalance(total=500, consumed=100),
            consumed_quantity=10,
        )

    def test_unlimited_balance_allowed(self):
        # total=-1 (unlimited) bypasses the exhaustion check; the
        # consume() function elsewhere is the no-op for unlimited.
        check_roaming_balance_required(
            subscription_id="SUB-001",
            balance=_FakeBalance(total=-1, consumed=999_999),
            consumed_quantity=10,
        )
