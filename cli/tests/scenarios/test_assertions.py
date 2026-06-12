"""Assertion evaluator + polling tests."""

from __future__ import annotations

import asyncio

from bss_cli.scenarios.assertions import (
    evaluate_expect,
    poll_until,
    resolve_path,
)
from bss_cli.scenarios.schema import Poll

# ─── path resolution ────────────────────────────────────────────────────────


def test_resolve_dict_path() -> None:
    assert resolve_path({"a": {"b": 7}}, "a.b") == 7


def test_resolve_list_by_numeric_index() -> None:
    assert resolve_path({"items": [10, 20, 30]}, "items.1") == 20


def test_resolve_list_by_matching_subkey() -> None:
    payload = {
        "balances": [
            {"allowanceType": "data", "remaining": 512, "unit": "mb"},
            {"allowanceType": "voice", "remaining": 0, "unit": "min"},
        ]
    }
    assert resolve_path(payload, "balances.data.remaining") == 512
    assert resolve_path(payload, "balances.voice.remaining") == 0


def test_resolve_missing_path_is_sentinel() -> None:
    from bss_cli.scenarios.assertions import _SENTINEL

    assert resolve_path({"a": 1}, "a.b") is _SENTINEL
    assert resolve_path({"a": [{"id": "X"}]}, "a.Y") is _SENTINEL


# ─── expect evaluation ─────────────────────────────────────────────────────


def test_expect_simple_equality() -> None:
    r = evaluate_expect({"state": "active"}, {"state": "active"})
    assert r.ok


def test_expect_equality_fails_with_reason() -> None:
    r = evaluate_expect({"state": "active"}, {"state": "blocked"})
    assert not r.ok
    assert r.failures[0].path == "state"
    assert r.failures[0].actual == "blocked"


def test_expect_gt_operator() -> None:
    r = evaluate_expect(
        {"balance.remaining": {"gt": 0}},
        {"balance": {"remaining": 5}},
    )
    assert r.ok
    r2 = evaluate_expect(
        {"balance.remaining": {"gt": 0}},
        {"balance": {"remaining": 0}},
    )
    assert not r2.ok


def test_expect_starts_with_operator() -> None:
    r = evaluate_expect(
        {"actor": {"starts_with": "llm-"}},
        {"actor": "llm-xiaomi-mimo"},
    )
    assert r.ok


def test_expect_any_match_over_list() -> None:
    interactions = [
        {"channel": "cli", "actor": "cli-user"},
        {"channel": "llm", "actor": "llm-mimo"},
    ]
    r = evaluate_expect(
        {"any_match": {"channel": "llm", "actor": {"starts_with": "llm-"}}},
        interactions,
    )
    assert r.ok


def test_expect_any_match_failure() -> None:
    r = evaluate_expect(
        {"any_match": {"channel": "llm"}},
        [{"channel": "cli"}],
    )
    assert not r.ok


def test_expect_balances_dot_path_list_match() -> None:
    # This is the PHASE_10 hero-scenario shape.
    payload = {
        "state": "active",
        "balances": [
            {"allowanceType": "data", "remaining": 5120, "unit": "mb"},
        ],
    }
    r = evaluate_expect(
        {"state": "active", "balances.data.remaining": 5120},
        payload,
    )
    assert r.ok


# ─── polling ────────────────────────────────────────────────────────────────


def test_poll_returns_first_success() -> None:
    state = {"n": 0}

    async def fetch():
        state["n"] += 1
        return {"state": "active" if state["n"] >= 3 else "pending"}

    async def _run():
        return await poll_until(
            fetch, {"state": "active"},
            Poll(interval_ms=5, timeout_seconds=1.0),
        )

    result = asyncio.run(_run())
    assert result.ok
    assert state["n"] == 3


def test_poll_times_out_and_surfaces_last_failure() -> None:
    async def fetch():
        return {"state": "pending"}

    async def _run():
        return await poll_until(
            fetch, {"state": "active"},
            Poll(interval_ms=5, timeout_seconds=0.05),
        )

    result = asyncio.run(_run())
    assert not result.ok
    assert result.failures[0].path == "state"


def test_poll_none_is_single_shot() -> None:
    async def fetch():
        return {"state": "active"}

    async def _run():
        return await poll_until(fetch, {"state": "active"}, None)

    assert asyncio.run(_run()).ok
