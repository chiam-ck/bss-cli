"""Unit tests for destructive-operation gating."""

from __future__ import annotations

import pytest
from bss_orchestrator.safety import (
    DESTRUCTIVE_TOOLS,
    is_destructive,
    make_loop_state,
    wrap_destructive,
)


def test_known_destructive_tools_flagged() -> None:
    for name in [
        "subscription.terminate",
        "order.cancel",
        "payment.remove_method",
        "case.close",
    ]:
        assert is_destructive(name), name


def test_readonly_tools_not_flagged() -> None:
    for name in ["customer.get", "subscription.get", "catalog.list_offerings"]:
        assert not is_destructive(name), name


@pytest.mark.asyncio
async def test_wrap_destructive_blocks_by_default() -> None:
    called = False

    async def terminate(**kwargs):
        nonlocal called
        called = True
        return {"id": kwargs["subscription_id"], "state": "terminated"}

    gated = wrap_destructive(
        terminate, tool_name="subscription.terminate", allow_destructive=False
    )
    result = await gated(subscription_id="SUB-007")
    assert result["error"] == "DESTRUCTIVE_OPERATION_BLOCKED"
    assert result["tool"] == "subscription.terminate"
    assert called is False  # underlying fn must not run


@pytest.mark.asyncio
async def test_wrap_destructive_runs_when_allowed() -> None:
    async def terminate(**kwargs):
        return {"id": kwargs["subscription_id"], "state": "terminated"}

    gated = wrap_destructive(
        terminate, tool_name="subscription.terminate", allow_destructive=True
    )
    result = await gated(subscription_id="SUB-007")
    assert result == {"id": "SUB-007", "state": "terminated"}


@pytest.mark.asyncio
async def test_wrap_destructive_returns_fn_unchanged_for_read_tools() -> None:
    async def get(**kwargs):
        return {"id": kwargs["customer_id"]}

    wrapped = wrap_destructive(
        get, tool_name="customer.get", allow_destructive=False
    )
    # Non-destructive: returned untouched (same object).
    assert wrapped is get


def test_destructive_set_matches_doctrine() -> None:
    # Sanity — the gated list must include the doctrine-enumerated destructive ops.
    required = {
        "subscription.terminate",
        "order.cancel",
        "payment.remove_method",
        "case.close",
        "ticket.cancel",
        "customer.close",
    }
    assert required.issubset(DESTRUCTIVE_TOOLS)


# ─── v1.5 autonomy-aware gating ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_granular_blocks_second_destructive_even_when_allowed() -> None:
    """The whole point of granular mode — once a destructive tool fires,
    subsequent destructive tools in the same graph re-gate as if
    allow_destructive=False, so the LLM is forced to re-propose and the
    operator must /confirm again."""
    state = make_loop_state()
    call_log: list[str] = []

    async def terminate(**kwargs):
        call_log.append(f"terminate:{kwargs['subscription_id']}")
        return {"ok": kwargs["subscription_id"]}

    async def cancel(**kwargs):
        call_log.append(f"cancel:{kwargs['order_id']}")
        return {"ok": kwargs["order_id"]}

    gated_terminate = wrap_destructive(
        terminate,
        tool_name="subscription.terminate",
        allow_destructive=True,
        autonomy_mode="granular",
        loop_state=state,
    )
    gated_cancel = wrap_destructive(
        cancel,
        tool_name="order.cancel",
        allow_destructive=True,
        autonomy_mode="granular",
        loop_state=state,
    )

    # First destructive: executes.
    first = await gated_terminate(subscription_id="SUB-001")
    assert first == {"ok": "SUB-001"}
    assert call_log == ["terminate:SUB-001"]

    # Second destructive in same loop: blocks.
    second = await gated_cancel(order_id="ORD-002")
    assert second["error"] == "DESTRUCTIVE_OPERATION_BLOCKED"
    assert second["tool"] == "order.cancel"
    # Underlying fn did NOT run — call_log unchanged.
    assert call_log == ["terminate:SUB-001"]


@pytest.mark.asyncio
async def test_batched_runs_every_destructive_once_allowed() -> None:
    """Batched mode is the pre-v1.5 behaviour — /confirm authorises the
    whole loop, so every destructive in the same graph executes."""
    state = make_loop_state()
    call_log: list[str] = []

    async def terminate(**kwargs):
        call_log.append(f"terminate:{kwargs['subscription_id']}")
        return {"ok": kwargs["subscription_id"]}

    async def cancel(**kwargs):
        call_log.append(f"cancel:{kwargs['order_id']}")
        return {"ok": kwargs["order_id"]}

    gated_terminate = wrap_destructive(
        terminate,
        tool_name="subscription.terminate",
        allow_destructive=True,
        autonomy_mode="batched",
        loop_state=state,
    )
    gated_cancel = wrap_destructive(
        cancel,
        tool_name="order.cancel",
        allow_destructive=True,
        autonomy_mode="batched",
        loop_state=state,
    )

    await gated_terminate(subscription_id="SUB-001")
    await gated_cancel(order_id="ORD-002")
    assert call_log == ["terminate:SUB-001", "cancel:ORD-002"]


@pytest.mark.asyncio
async def test_granular_disallowed_blocks_first_destructive_too() -> None:
    """Sanity: granular mode doesn't accidentally relax the disallowed
    case. allow_destructive=False blocks every destructive regardless
    of how many have run (or not)."""
    state = make_loop_state()
    called = False

    async def terminate(**kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    gated = wrap_destructive(
        terminate,
        tool_name="subscription.terminate",
        allow_destructive=False,
        autonomy_mode="granular",
        loop_state=state,
    )
    result = await gated(subscription_id="SUB-001")
    assert result["error"] == "DESTRUCTIVE_OPERATION_BLOCKED"
    assert called is False
    # And the counter stays at zero — a blocked call doesn't count as
    # "destructive fired".
    assert state["destructive_executed"] == 0


@pytest.mark.asyncio
async def test_granular_non_destructive_calls_do_not_consume_budget() -> None:
    """A `customer.get` between two destructive calls in granular mode
    must not affect the first-destructive budget — the gate is for
    destructive tools only."""
    state = make_loop_state()

    async def terminate(**kwargs):
        return {"ok": True}

    async def get(**kwargs):
        return {"id": kwargs["customer_id"]}

    gated_terminate = wrap_destructive(
        terminate,
        tool_name="subscription.terminate",
        allow_destructive=True,
        autonomy_mode="granular",
        loop_state=state,
    )
    # Non-destructive: returned unchanged. Sanity-call to prove the
    # untouched function path doesn't share state.
    wrapped_get = wrap_destructive(
        get,
        tool_name="customer.get",
        allow_destructive=True,
        autonomy_mode="granular",
        loop_state=state,
    )
    assert wrapped_get is get

    await wrapped_get(customer_id="CUST-001")
    # Counter still zero after a read.
    assert state["destructive_executed"] == 0

    await gated_terminate(subscription_id="SUB-001")
    assert state["destructive_executed"] == 1


@pytest.mark.asyncio
async def test_fresh_loop_state_resets_between_graph_builds() -> None:
    """Each ``build_graph`` builds a fresh ``LoopState`` — a second
    astream_once turn gets a clean budget. Simulated here by calling
    ``make_loop_state`` twice."""
    async def terminate(**kwargs):
        return {"ok": True}

    # Loop 1 — terminate, then second destructive blocks.
    state1 = make_loop_state()
    gated1 = wrap_destructive(
        terminate,
        tool_name="subscription.terminate",
        allow_destructive=True,
        autonomy_mode="granular",
        loop_state=state1,
    )
    await gated1(subscription_id="SUB-001")
    second = await gated1(subscription_id="SUB-002")
    assert second["error"] == "DESTRUCTIVE_OPERATION_BLOCKED"

    # Loop 2 (= fresh graph build) — first destructive fires again.
    state2 = make_loop_state()
    gated2 = wrap_destructive(
        terminate,
        tool_name="subscription.terminate",
        allow_destructive=True,
        autonomy_mode="granular",
        loop_state=state2,
    )
    result = await gated2(subscription_id="SUB-003")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_default_autonomy_is_batched_for_backcompat() -> None:
    """v1.5 lands "granular" as the orchestrator-process default, but the
    safety primitive's own default is "batched" so the dozens of test
    paths and scenario callers that don't pass autonomy_mode keep their
    pre-v1.5 behaviour. Production cockpit callers pass the mode
    explicitly from ``read_autonomy_mode()``."""
    state = make_loop_state()

    async def terminate(**kwargs):
        return {"ok": True}

    gated = wrap_destructive(
        terminate,
        tool_name="subscription.terminate",
        allow_destructive=True,
        # autonomy_mode not passed — verifies the default.
        loop_state=state,
    )
    # Two destructives in a row both run under the implicit batched default.
    assert await gated(subscription_id="SUB-001") == {"ok": True}
    assert await gated(subscription_id="SUB-002") == {"ok": True}
