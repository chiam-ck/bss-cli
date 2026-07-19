"""v1.5 — 3-strike bail for tool-failure loops.

Tests the failure-classification helper directly and the loop-counter
behaviour via a stubbed graph that returns failure-shaped ToolMessages.
The classifier is the small surface most likely to drift; the loop
integration is exercised end-to-end in the v1.5 e2e suite (Phase E).
"""

from __future__ import annotations

from bss_orchestrator.session import (
    MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS,
    MAX_CONSECUTIVE_TOOL_FAILURES,
    _IdenticalCallTracker,
    _is_failure_tool_result,
    _tool_args_sig,
)

# ─── _is_failure_tool_result classifier ──────────────────────────────────


def test_status_error_is_failure() -> None:
    # LangGraph's own exception path — ToolMessage.status="error".
    assert _is_failure_tool_result("anything here", is_error=True) is True


def test_policy_violation_is_failure() -> None:
    body = (
        '{"error": "POLICY_VIOLATION", "rule": "case.close.requires_all_'
        'tickets_resolved", "detail": "..."}'
    )
    assert _is_failure_tool_result(body, is_error=False) is True


def test_destructive_blocked_is_failure() -> None:
    body = (
        '{"error": "DESTRUCTIVE_OPERATION_BLOCKED", "tool": '
        '"subscription.terminate", "message": "..."}'
    )
    assert _is_failure_tool_result(body, is_error=False) is True


def test_client_error_is_failure() -> None:
    body = '{"error": "CLIENT_ERROR", "status": 503, "detail": "..."}'
    assert _is_failure_tool_result(body, is_error=False) is True


def test_normal_tool_result_is_not_failure() -> None:
    body = '{"id": "CUST-001", "name": "Alice", "state": "active"}'
    assert _is_failure_tool_result(body, is_error=False) is False


def test_empty_result_is_not_failure() -> None:
    # An empty body from a void-returning tool should NOT trip the counter.
    assert _is_failure_tool_result("", is_error=False) is False


def test_unrelated_word_error_is_not_failure() -> None:
    # The marker matches the exact JSON-key shape, not any mention of
    # "error" in prose — a tool that returns a customer's email field
    # like "user-error@example.com" must not be flagged.
    body = '{"email": "user-error@example.com", "id": "CUST-002"}'
    assert _is_failure_tool_result(body, is_error=False) is False


# ─── _IdenticalCallTracker (v1.6.2 stuck-loop bail) ──────────────────────
#
# The 2026-06-12 incident: customer.list(name_contains=<email>) → []
# replayed verbatim. Each replay was a SUCCESS so the failure counter
# above reset every time; the tracker counts identical
# (tool, args, result) triples instead.


def test_three_identical_calls_trip() -> None:
    t = _IdenticalCallTracker()
    sig = _tool_args_sig({"name_contains": "x@example.com"})
    assert t.record("customer.list", sig, "[]") is False
    assert t.record("customer.list", sig, "[]") is False
    assert t.record("customer.list", sig, "[]") is True


def test_changed_result_resets_the_run() -> None:
    # A poll whose target progresses is investigation, not replay.
    t = _IdenticalCallTracker()
    sig = _tool_args_sig({"task_id": "PTK-1"})
    assert t.record("provisioning.get_task", sig, '{"state": "pending"}') is False
    assert t.record("provisioning.get_task", sig, '{"state": "pending"}') is False
    assert t.record("provisioning.get_task", sig, '{"state": "completed"}') is False


def test_changed_args_reset_the_run() -> None:
    t = _IdenticalCallTracker()
    assert t.record("customer.list", _tool_args_sig({"name_contains": "a"}), "[]") is False
    assert t.record("customer.list", _tool_args_sig({"name_contains": "b"}), "[]") is False
    assert t.record("customer.list", _tool_args_sig({"name_contains": "c"}), "[]") is False


def test_interleaved_other_call_resets_the_run() -> None:
    t = _IdenticalCallTracker()
    sig = _tool_args_sig({"name_contains": "x"})
    assert t.record("customer.list", sig, "[]") is False
    assert t.record("clock.now", _tool_args_sig({}), '"2026-06-12"') is False
    assert t.record("customer.list", sig, "[]") is False
    assert t.record("customer.list", sig, "[]") is False


def test_args_sig_is_key_order_independent() -> None:
    assert _tool_args_sig({"a": 1, "b": 2}) == _tool_args_sig({"b": 2, "a": 1})


def test_args_sig_never_raises() -> None:
    # Unserialisable arg values must not break the stream over a counter.
    assert isinstance(_tool_args_sig({"x": object()}), str)
    assert isinstance(_tool_args_sig(None), str)


# ─── Constant guards ─────────────────────────────────────────────────────


def test_bail_threshold_stays_at_three() -> None:
    # Three is the lift from loyalty-cli's pattern. If someone changes
    # this they should review the test that exercises the actual bail
    # (e2e) and the prompt doctrine that depends on this being a
    # "small, predictable" number.
    assert MAX_CONSECUTIVE_TOOL_FAILURES == 3


def test_identical_call_threshold_stays_at_three() -> None:
    # Same review obligations as the failure threshold: the cockpit's
    # "couldn't recover" panel copy and the soak corpus assume a small,
    # predictable bail point.
    assert MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS == 3
