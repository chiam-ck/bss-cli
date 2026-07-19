"""Per-customer chat caps (v0.12 PR5).

Five concerns:

1. ``_cost_cents_for_turn`` — token counts × per-model rate, integer cents.
2. ``_HourlyWindow`` — sliding-window prune semantics.
3. ``check_caps`` allowed/blocked paths, including month-boundary rollover.
4. ``record_chat_turn`` increments hourly + invokes the upsert.
5. **Fail closed** — when the DB raises, ``check_caps`` returns
   ``allowed=False, reason="cap_check_failed"``, never True.

DB-touching tests mock ``_get_engine`` rather than hit Postgres — the
migration was verified end-to-end in PR1; here we're testing the
caps logic against the contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bss_orchestrator import chat_caps
from bss_orchestrator.chat_caps import (
    MODEL_RATES_USD_PER_M_TOK,
    CapStatus,
    _cost_cents_for_turn,
    _HourlyWindow,
    _period_yyyymm,
    check_caps,
    record_chat_turn,
)

# ─── 1. Cost accounting ─────────────────────────────────────────────


def test_known_model_cost_uses_rate_table() -> None:
    # Gemma 4 26B A4B is in the table. (0.15, 0.50) per 1M tokens.
    cents = _cost_cents_for_turn(
        model="google/gemma-4-26b-a4b-it",
        prompt_tok=1_000_000,
        completion_tok=1_000_000,
    )
    # 0.15 + 0.50 = 0.65 USD = 65 cents (rounded up).
    assert 64 <= cents <= 66


def test_unknown_model_falls_back_to_configured_rate() -> None:
    cents = _cost_cents_for_turn(
        model="some/uncatalogued-model", prompt_tok=1_000_000, completion_tok=0
    )
    expected, _ = MODEL_RATES_USD_PER_M_TOK[
        next(iter(MODEL_RATES_USD_PER_M_TOK))
    ]
    assert cents >= int(expected * 100)


def test_zero_tokens_yields_zero_cents() -> None:
    cents = _cost_cents_for_turn(
        model="google/gemma-4-26b-a4b-it",
        prompt_tok=0,
        completion_tok=0,
    )
    assert cents == 0


def test_partial_cents_always_round_up() -> None:
    # Doctrine: any positive cost rounds up to 1 cent so nano-turns
    # always count toward the cap. Under-counting is the only failure
    # mode that matters here — the cap is a soft ceiling, not a bill.
    cents = _cost_cents_for_turn(
        model="google/gemma-4-26b-a4b-it", prompt_tok=100, completion_tok=100
    )
    assert cents == 1


# ─── 2. Sliding window ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hourly_window_records_and_counts() -> None:
    win = _HourlyWindow(timedelta(hours=1))
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)

    assert await win.count("CUST-1", now=t0) == 0
    assert await win.record("CUST-1", t0) == 1
    assert await win.record("CUST-1", t0 + timedelta(minutes=10)) == 2
    assert await win.count("CUST-1", now=t0 + timedelta(minutes=10)) == 2


@pytest.mark.asyncio
async def test_hourly_window_prunes_old_entries() -> None:
    win = _HourlyWindow(timedelta(hours=1))
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    await win.record("CUST-1", t0)
    # 70 minutes later — the original entry is outside the window.
    later = t0 + timedelta(minutes=70)
    assert await win.count("CUST-1", now=later) == 0


@pytest.mark.asyncio
async def test_hourly_window_isolates_customers() -> None:
    win = _HourlyWindow(timedelta(hours=1))
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    await win.record("CUST-1", t0)
    await win.record("CUST-2", t0)
    assert await win.count("CUST-1", now=t0) == 1
    assert await win.count("CUST-2", now=t0) == 1


# ─── 3. check_caps ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _reset_window():
    await chat_caps._per_customer.reset()
    await chat_caps._per_ip.reset()
    yield
    await chat_caps._per_customer.reset()
    await chat_caps._per_ip.reset()


@pytest.mark.asyncio
async def test_check_caps_allows_fresh_customer() -> None:
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    with patch.object(chat_caps, "_read_month_cost_cents", AsyncMock(return_value=0)):
        status = await check_caps("CUST-FRESH", now=t0)
    assert status == CapStatus(allowed=True)


@pytest.mark.asyncio
async def test_check_caps_blocks_when_hourly_rate_exceeded() -> None:
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    # Pre-record 20 turns in the window (default cap).
    for i in range(20):
        await chat_caps._per_customer.record(
            "CUST-HOT", t0 - timedelta(minutes=i)
        )
    with patch.object(chat_caps, "_read_month_cost_cents", AsyncMock(return_value=0)):
        status = await check_caps("CUST-HOT", now=t0)
    assert status.allowed is False
    assert status.reason == "hourly_rate_cap"
    assert status.retry_at == t0 + timedelta(hours=1)


@pytest.mark.asyncio
async def test_check_caps_blocks_when_monthly_cost_exceeded() -> None:
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    # Default cap is 200 cents; pretend the customer already burned 200.
    with patch.object(
        chat_caps, "_read_month_cost_cents", AsyncMock(return_value=200)
    ):
        status = await check_caps("CUST-BURNT", now=t0)
    assert status.allowed is False
    assert status.reason == "monthly_cost_cap"
    # retry_at is the start of the following month.
    assert status.retry_at == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_check_caps_handles_year_rollover() -> None:
    t0 = datetime(2026, 12, 15, 12, 0, tzinfo=timezone.utc)
    with patch.object(
        chat_caps, "_read_month_cost_cents", AsyncMock(return_value=200)
    ):
        status = await check_caps("CUST-DEC", now=t0)
    assert status.retry_at == datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_check_caps_fails_closed_on_db_error() -> None:
    """Doctrine: a cap that doesn't enforce is worse than no cap."""
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)

    async def _broken(*_a, **_kw):
        raise RuntimeError("postgres unreachable")

    with patch.object(chat_caps, "_read_month_cost_cents", _broken):
        status = await check_caps("CUST-X", now=t0)
    assert status.allowed is False
    assert status.reason == "cap_check_failed"


# ─── 4. record_chat_turn ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_chat_turn_appends_hourly_and_writes_db() -> None:
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)

    fake_conn = AsyncMock()
    fake_engine = MagicMock()

    class _BeginCM:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, *_a):
            return False

    fake_engine.begin = MagicMock(return_value=_BeginCM())

    with patch.object(chat_caps, "_get_engine", AsyncMock(return_value=fake_engine)):
        await record_chat_turn(
            customer_id="CUST-RECORDED",
            prompt_tok=1_000_000,
            completion_tok=1_000_000,
            model="google/gemma-4-26b-a4b-it",
            now=t0,
        )

    # Hourly window now reflects one entry for this customer.
    assert await chat_caps._per_customer.count("CUST-RECORDED", now=t0) == 1

    # The upsert was called with the right cost + period.
    fake_conn.execute.assert_called_once()
    args = fake_conn.execute.call_args[0]
    params = args[1]
    assert params["cid"] == "CUST-RECORDED"
    assert params["p"] == _period_yyyymm(t0)
    assert params["cost"] >= 64  # ~65 cents per the rate table


@pytest.mark.asyncio
async def test_record_chat_turn_swallows_db_errors() -> None:
    """A failed accounting write must not error a successful chat
    turn after the LLM already responded."""
    t0 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)

    async def _broken(*_a, **_kw):
        raise RuntimeError("postgres exploded mid-write")

    with patch.object(chat_caps, "_get_engine", _broken):
        # Must not raise.
        await record_chat_turn(
            customer_id="CUST-X",
            prompt_tok=100,
            completion_tok=200,
            now=t0,
        )

    # Hourly window still recorded the turn even though DB failed —
    # the in-memory window cannot fail and the rate cap stays honest.
    assert await chat_caps._per_customer.count("CUST-X", now=t0) == 1


# ─── 5. Period helpers ──────────────────────────────────────────────


def test_period_yyyymm_format() -> None:
    assert _period_yyyymm(datetime(2026, 4, 27)) == 202604
    assert _period_yyyymm(datetime(2027, 1, 1)) == 202701
    assert _period_yyyymm(datetime(2026, 12, 31)) == 202612
