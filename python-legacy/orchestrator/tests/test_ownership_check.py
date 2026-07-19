"""Output ownership trip-wire (v0.12 PR4).

Three classes of assertion:

1. ``assert_owned_output`` — recognises good payloads, trips on bad
   ones, tolerates non-JSON, skips unknown tools.
2. ``OWNERSHIP_PATHS`` registry covers every tool in the
   ``customer_self_serve`` profile (startup self-check).
3. ``astream_once`` integration — a planted bad ToolMessage trips the
   stream into ``AgentEventError`` with no leaked content; a clean
   stream proceeds normally; the check is gated on actor + non-error.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from bss_orchestrator import auth_context
from bss_orchestrator.ownership import (
    OWNERSHIP_PATHS,
    AgentOwnershipViolation,
    assert_owned_output,
    validate_ownership_paths_cover_profile,
)
from bss_orchestrator.session import (
    AgentEventError,
    AgentEventToolCallCompleted,
    astream_once,
)
from bss_orchestrator.tools import TOOL_PROFILES
from langchain_core.messages import AIMessage, ToolMessage

# ─── 1. assert_owned_output unit tests ──────────────────────────────


def test_owned_top_level_id_passes() -> None:
    payload = json.dumps({"id": "CUST-042", "status": "active"})
    assert_owned_output(
        tool_name="customer.get_mine",
        result_json=payload,
        actor="CUST-042",
    )


def test_owned_nested_customer_id_passes() -> None:
    payload = json.dumps(
        {"id": "SUB-7", "customerId": "CUST-042", "state": "active"}
    )
    assert_owned_output(
        tool_name="subscription.get_mine",
        result_json=payload,
        actor="CUST-042",
    )


def test_planted_bad_top_level_id_trips() -> None:
    payload = json.dumps({"id": "CUST-OTHER"})
    with pytest.raises(AgentOwnershipViolation) as exc_info:
        assert_owned_output(
            tool_name="customer.get_mine",
            result_json=payload,
            actor="CUST-042",
        )
    assert exc_info.value.tool_name == "customer.get_mine"
    assert exc_info.value.found == "CUST-OTHER"
    assert exc_info.value.actor == "CUST-042"


def test_planted_bad_customer_id_trips() -> None:
    payload = json.dumps({"id": "SUB-7", "customerId": "CUST-OTHER"})
    with pytest.raises(AgentOwnershipViolation):
        assert_owned_output(
            tool_name="subscription.get_mine",
            result_json=payload,
            actor="CUST-042",
        )


def test_list_iter_path_owned_passes() -> None:
    payload = json.dumps(
        [
            {"id": "SUB-1", "customerId": "CUST-042"},
            {"id": "SUB-2", "customerId": "CUST-042"},
        ]
    )
    assert_owned_output(
        tool_name="subscription.list_mine",
        result_json=payload,
        actor="CUST-042",
    )


def test_list_iter_path_one_alien_trips() -> None:
    payload = json.dumps(
        [
            {"id": "SUB-1", "customerId": "CUST-042"},
            {"id": "SUB-99", "customerId": "CUST-OTHER"},
        ]
    )
    with pytest.raises(AgentOwnershipViolation) as exc_info:
        assert_owned_output(
            tool_name="subscription.list_mine",
            result_json=payload,
            actor="CUST-042",
        )
    assert exc_info.value.found == "CUST-OTHER"


def test_unknown_tool_does_not_trip() -> None:
    """Conservative default — a tool name not in OWNERSHIP_PATHS is
    treated as 'no customer-bound output'. The startup self-check
    enforces that profile tools have entries; tools outside the
    profile (canonical reads, scenario tools, etc.) bypass the
    check naturally."""
    payload = json.dumps({"customerId": "CUST-OTHER"})
    assert_owned_output(
        tool_name="some.unknown_tool",
        result_json=payload,
        actor="CUST-042",
    )


def test_empty_paths_entry_does_not_trip() -> None:
    payload = json.dumps({"customerId": "CUST-OTHER"})
    # subscription.get_balance_mine is configured with [] — no
    # customer-bound paths to assert.
    assert_owned_output(
        tool_name="subscription.get_balance_mine",
        result_json=payload,
        actor="CUST-042",
    )


def test_non_json_result_tolerated() -> None:
    # Tool error / plain string results cannot carry customer-bound
    # rows by definition; the check must not crash trying to parse them.
    assert_owned_output(
        tool_name="customer.get_mine",
        result_json="customer.get_mine raised PolicyViolation",
        actor="CUST-042",
    )


def test_missing_path_does_not_trip() -> None:
    """Some canonical tools omit fields conditionally; a missing path
    must not be conflated with an ownership violation."""
    payload = json.dumps({"id": "SUB-7"})  # no customerId field
    assert_owned_output(
        tool_name="subscription.get_mine",
        result_json=payload,
        actor="CUST-042",
    )


# ─── 2. OWNERSHIP_PATHS coverage of the profile ─────────────────────


def test_every_customer_profile_tool_has_ownership_paths_entry() -> None:
    profile = TOOL_PROFILES["customer_self_serve"]
    missing = sorted(t for t in profile if t not in OWNERSHIP_PATHS)
    assert not missing, (
        f"OWNERSHIP_PATHS missing entries for {missing!r}. Add an "
        f"explicit list (use [] when the tool's response carries no "
        f"customer-bound fields by contract)."
    )


def test_validate_ownership_paths_raises_on_missing_entry() -> None:
    with pytest.raises(RuntimeError, match="OWNERSHIP_PATHS is missing"):
        validate_ownership_paths_cover_profile({"definitely.not.in.paths"})


def test_validate_ownership_paths_accepts_full_profile() -> None:
    validate_ownership_paths_cover_profile(TOOL_PROFILES["customer_self_serve"])


# ─── 3. astream_once integration ────────────────────────────────────


class _FakeGraph:
    def __init__(self, updates: list[dict[str, Any]]):
        self._updates = updates

    async def astream(
        self, _input: Any, *, stream_mode: str = "updates"
    ) -> AsyncIterator[dict[str, Any]]:
        for u in self._updates:
            yield u


async def _collect(stream) -> list:
    return [ev async for ev in stream]


@pytest.fixture(autouse=True)
def _clear_actor_after():
    yield
    token = auth_context.set_actor("__test_clear__")
    auth_context.reset_actor(token)


@pytest.mark.asyncio
async def test_clean_tool_result_does_not_trip_stream() -> None:
    fake = _FakeGraph(
        [
            {"agent": {"messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "customer.get_mine",
                            "args": {},
                            "id": "call_1",
                        }
                    ],
                )
            ]}},
            {"tools": {"messages": [
                ToolMessage(
                    content=json.dumps({"id": "CUST-042", "status": "active"}),
                    name="customer.get_mine",
                    tool_call_id="call_1",
                )
            ]}},
            {"agent": {"messages": [AIMessage(content="Your account is active.")]}},
        ]
    )

    fake_clients = AsyncMock()
    with patch("bss_orchestrator.session.build_graph", return_value=fake), patch(
        "bss_orchestrator.session.get_clients", return_value=fake_clients
    ):
        events = await _collect(
            astream_once(
                "what's my status",
                channel="portal-self-serve",
                actor="CUST-042",
                tool_filter="customer_self_serve",
            )
        )

    assert not any(isinstance(e, AgentEventError) for e in events)
    assert any(isinstance(e, AgentEventToolCallCompleted) for e in events)
    fake_clients.crm.log_interaction.assert_not_called()


@pytest.mark.asyncio
async def test_planted_bad_tool_result_trips_into_error_event() -> None:
    fake = _FakeGraph(
        [
            {"agent": {"messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "customer.get_mine",
                            "args": {},
                            "id": "call_1",
                        }
                    ],
                )
            ]}},
            # The tool returns CUST-OTHER even though the actor is CUST-042
            # — simulates a hypothetical server-side leak past the
            # policy layer. The trip-wire must catch it.
            {"tools": {"messages": [
                ToolMessage(
                    content=json.dumps({"id": "CUST-OTHER"}),
                    name="customer.get_mine",
                    tool_call_id="call_1",
                )
            ]}},
            {"agent": {"messages": [AIMessage(content="should not be reached")]}},
        ]
    )

    fake_clients = AsyncMock()
    with patch("bss_orchestrator.session.build_graph", return_value=fake), patch(
        "bss_orchestrator.session.get_clients", return_value=fake_clients
    ):
        events = await _collect(
            astream_once(
                "what's my status",
                channel="portal-self-serve",
                actor="CUST-042",
                tool_filter="customer_self_serve",
            )
        )

    errors = [e for e in events if isinstance(e, AgentEventError)]
    assert len(errors) == 1
    assert "AgentOwnershipViolation" in errors[0].message
    assert "customer.get_mine" in errors[0].message
    # Audit trail emitted on the actor's record.
    fake_clients.crm.log_interaction.assert_called_once()
    log_args = fake_clients.crm.log_interaction.call_args.kwargs
    assert log_args["customer_id"] == "CUST-042"
    assert "customer.get_mine" in log_args["summary"]


@pytest.mark.asyncio
async def test_error_status_tool_result_skips_check() -> None:
    """Error-status ToolMessages (PolicyViolation observation strings)
    must not trip the wire — they're already the canonical failure
    path and don't carry customer-bound rows."""
    fake = _FakeGraph(
        [
            {"agent": {"messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "customer.get_mine",
                            "args": {},
                            "id": "call_1",
                        }
                    ],
                )
            ]}},
            {"tools": {"messages": [
                ToolMessage(
                    content=json.dumps(
                        {"error": "POLICY_VIOLATION", "id": "CUST-OTHER"}
                    ),
                    name="customer.get_mine",
                    tool_call_id="call_1",
                    status="error",
                )
            ]}},
            {"agent": {"messages": [AIMessage(content="something went wrong")]}},
        ]
    )

    fake_clients = AsyncMock()
    with patch("bss_orchestrator.session.build_graph", return_value=fake), patch(
        "bss_orchestrator.session.get_clients", return_value=fake_clients
    ):
        events = await _collect(
            astream_once(
                "ping",
                channel="portal-self-serve",
                actor="CUST-042",
                tool_filter="customer_self_serve",
            )
        )

    # No ownership trip even though id=CUST-OTHER is in the error payload.
    assert not any(
        isinstance(e, AgentEventError)
        and "AgentOwnershipViolation" in getattr(e, "message", "")
        for e in events
    )


@pytest.mark.asyncio
async def test_no_actor_bypasses_check() -> None:
    """When actor is not bound (CLI, scenario, CSR), the trip-wire is
    inert. Those callers operate on multiple customers and the check
    would mis-fire. Server-side policies remain the boundary for them."""
    fake = _FakeGraph(
        [
            {"agent": {"messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "customer.get_mine",
                            "args": {},
                            "id": "call_1",
                        }
                    ],
                )
            ]}},
            {"tools": {"messages": [
                ToolMessage(
                    content=json.dumps({"id": "CUST-OTHER"}),
                    name="customer.get_mine",
                    tool_call_id="call_1",
                )
            ]}},
            {"agent": {"messages": [AIMessage(content="ok")]}},
        ]
    )

    fake_clients = AsyncMock()
    with patch("bss_orchestrator.session.build_graph", return_value=fake), patch(
        "bss_orchestrator.session.get_clients", return_value=fake_clients
    ):
        events = await _collect(astream_once("ping"))

    assert not any(
        isinstance(e, AgentEventError)
        and "AgentOwnershipViolation" in getattr(e, "message", "")
        for e in events
    )
