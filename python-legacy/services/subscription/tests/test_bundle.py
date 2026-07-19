"""Tests for bundle.py pure functions — 100% branch coverage required."""

import pytest
from app.domain.bundle import (
    UNLIMITED,
    AllowanceSpec,
    BalanceSnapshot,
    add_allowance,
    consume,
    is_exhausted,
    primary_allowance_type,
    reset_for_new_period,
)
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

# ── Fixtures ────────────────────────────────────────────────────────


def _bal(
    total: int = 5120, consumed: int = 0, atype: str = "data", unit: str = "mb"
) -> BalanceSnapshot:
    return BalanceSnapshot(allowance_type=atype, total=total, consumed=consumed, unit=unit)


def _unlimited(atype: str = "data", unit: str = "mb") -> BalanceSnapshot:
    return BalanceSnapshot(allowance_type=atype, total=UNLIMITED, consumed=0, unit=unit)


# ── consume ─────────────────────────────────────────────────────────


class TestConsume:
    def test_normal_decrement(self):
        b = _bal(total=5120, consumed=0)
        result = consume(b, 100)
        assert result.consumed == 100
        assert result.remaining == 5020

    def test_consume_exact_remaining(self):
        b = _bal(total=5120, consumed=5000)
        result = consume(b, 120)
        assert result.consumed == 5120
        assert result.remaining == 0

    def test_consume_more_than_remaining_clamps(self):
        b = _bal(total=5120, consumed=5000)
        result = consume(b, 500)
        assert result.consumed == 5120
        assert result.remaining == 0

    def test_consume_zero_no_change(self):
        b = _bal(total=5120, consumed=100)
        result = consume(b, 0)
        assert result is b  # same object returned

    def test_consume_negative_raises(self):
        b = _bal()
        with pytest.raises(ValueError, match="non-negative"):
            consume(b, -1)

    def test_consume_unlimited_unchanged(self):
        b = _unlimited()
        result = consume(b, 9999)
        assert result is b
        assert result.consumed == 0

    def test_consume_already_exhausted(self):
        b = _bal(total=100, consumed=100)
        result = consume(b, 50)
        assert result.consumed == 100
        assert result.remaining == 0


# ── is_exhausted ────────────────────────────────────────────────────


class TestIsExhausted:
    def test_data_remaining_positive(self):
        balances = [_bal(total=5120, consumed=100)]
        assert is_exhausted(balances) is False

    def test_data_remaining_zero(self):
        balances = [_bal(total=5120, consumed=5120)]
        assert is_exhausted(balances) is True

    def test_data_unlimited_never_exhausts(self):
        balances = [_unlimited("data")]
        assert is_exhausted(balances) is False

    def test_voice_exhausted_but_data_positive(self):
        balances = [
            _bal(total=5120, consumed=100, atype="data"),
            _bal(total=100, consumed=100, atype="voice", unit="minutes"),
        ]
        assert is_exhausted(balances) is False

    def test_empty_balance_list(self):
        assert is_exhausted([]) is True

    def test_no_primary_type_match(self):
        balances = [_bal(total=100, consumed=0, atype="voice", unit="minutes")]
        assert is_exhausted(balances) is True

    def test_custom_primary_type(self):
        balances = [
            _bal(total=100, consumed=100, atype="voice", unit="minutes"),
            _bal(total=5120, consumed=0, atype="data"),
        ]
        assert is_exhausted(balances, primary_type="voice") is True
        assert is_exhausted(balances, primary_type="data") is False


# ── add_allowance ───────────────────────────────────────────────────


class TestAddAllowance:
    def test_normal_add(self):
        b = _bal(total=5120, consumed=1000)
        result = add_allowance(b, 1024)
        assert result.total == 6144
        assert result.consumed == 1000
        assert result.remaining == 5144

    def test_add_to_unlimited_unchanged(self):
        b = _unlimited()
        result = add_allowance(b, 1024)
        assert result is b

    def test_add_zero_no_change(self):
        b = _bal(total=5120, consumed=0)
        result = add_allowance(b, 0)
        assert result is b

    def test_add_negative_raises(self):
        b = _bal()
        with pytest.raises(ValueError, match="non-negative"):
            add_allowance(b, -1)


# ── reset_for_new_period ────────────────────────────────────────────


class TestResetForNewPeriod:
    def test_three_specs(self):
        specs = [
            AllowanceSpec("data", 5120, "mb"),
            AllowanceSpec("voice", 100, "minutes"),
            AllowanceSpec("sms", 100, "count"),
        ]
        result = reset_for_new_period(specs)
        assert len(result) == 3
        for b in result:
            assert b.consumed == 0

        data_b = result[0]
        assert data_b.allowance_type == "data"
        assert data_b.total == 5120
        assert data_b.unit == "mb"

    def test_unlimited_spec(self):
        specs = [AllowanceSpec("voice", UNLIMITED, "minutes")]
        result = reset_for_new_period(specs)
        assert len(result) == 1
        assert result[0].total == UNLIMITED
        assert result[0].consumed == 0
        assert result[0].remaining == UNLIMITED

    def test_empty_specs(self):
        assert reset_for_new_period([]) == []


# ── primary_allowance_type ──────────────────────────────────────────


class TestPrimaryAllowanceType:
    def test_returns_data(self):
        assert primary_allowance_type() == "data"


# ── BalanceSnapshot.remaining property ──────────────────────────────


class TestBalanceSnapshotRemaining:
    def test_remaining_normal(self):
        b = _bal(total=1000, consumed=300)
        assert b.remaining == 700

    def test_remaining_unlimited(self):
        b = _unlimited()
        assert b.remaining == UNLIMITED


# ── Hypothesis property tests ───────────────────────────────────────

finite_balance = st.builds(
    BalanceSnapshot,
    allowance_type=st.just("data"),
    total=st.integers(min_value=0, max_value=10_000_000),
    consumed=st.integers(min_value=0, max_value=10_000_000),
    unit=st.just("mb"),
).filter(lambda b: b.consumed <= b.total)

non_neg_qty = st.integers(min_value=0, max_value=10_000_000)


class TestHypothesis:
    @given(balance=finite_balance, quantity=non_neg_qty)
    @h_settings(max_examples=200)
    def test_consume_invariant_bounds(self, balance: BalanceSnapshot, quantity: int):
        """consumed is always in [0, total] after consume."""
        result = consume(balance, quantity)
        assert 0 <= result.consumed <= result.total

    @given(balance=finite_balance, quantity=non_neg_qty)
    @h_settings(max_examples=200)
    def test_consume_monotonic(self, balance: BalanceSnapshot, quantity: int):
        """Consuming never decreases consumed."""
        result = consume(balance, quantity)
        assert result.consumed >= balance.consumed

    @given(balance=finite_balance, quantity=st.integers(min_value=0, max_value=1_000_000))
    @h_settings(max_examples=200)
    def test_add_consume_total_roundtrip(self, balance: BalanceSnapshot, quantity: int):
        """add_allowance(consume(b, q), q).total == b.total + q for finite balances."""
        consumed_b = consume(balance, quantity)
        added_b = add_allowance(consumed_b, quantity)
        assert added_b.total == balance.total + quantity

    @given(quantity=st.integers(min_value=1, max_value=10_000_000))
    @h_settings(max_examples=100)
    def test_unlimited_immunity(self, quantity: int):
        """Consuming from unlimited balance never changes consumed."""
        b = _unlimited()
        result = consume(b, quantity)
        assert result.consumed == 0

    @given(
        specs=st.lists(
            st.builds(
                AllowanceSpec,
                allowance_type=st.sampled_from(["data", "voice", "sms"]),
                quantity=st.integers(min_value=-1, max_value=1_000_000),
                unit=st.sampled_from(["mb", "minutes", "count"]),
            ),
            min_size=0,
            max_size=5,
        )
    )
    @h_settings(max_examples=100)
    def test_reset_always_zero_consumed(self, specs: list[AllowanceSpec]):
        """reset_for_new_period always produces consumed=0."""
        result = reset_for_new_period(specs)
        assert len(result) == len(specs)
        for b in result:
            assert b.consumed == 0
