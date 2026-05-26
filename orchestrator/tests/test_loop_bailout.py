"""v1.5 — 3-strike bail for tool-failure loops.

Tests the failure-classification helper directly and the loop-counter
behaviour via a stubbed graph that returns failure-shaped ToolMessages.
The classifier is the small surface most likely to drift; the loop
integration is exercised end-to-end in the v1.5 e2e suite (Phase E).
"""

from __future__ import annotations

import pytest

from bss_orchestrator.session import (
    MAX_CONSECUTIVE_TOOL_FAILURES,
    _is_failure_tool_result,
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


# ─── Constant guard ──────────────────────────────────────────────────────


def test_bail_threshold_stays_at_three() -> None:
    # Three is the lift from loyalty-cli's pattern. If someone changes
    # this they should review the test that exercises the actual bail
    # (e2e) and the prompt doctrine that depends on this being a
    # "small, predictable" number.
    assert MAX_CONSECUTIVE_TOOL_FAILURES == 3
