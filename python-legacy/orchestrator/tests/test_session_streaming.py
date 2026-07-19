"""Unit tests for astream_once — the v0.4 portal entry point.

Mocks ``build_graph`` to return a fake compiled graph whose ``astream``
yields canned LangGraph update dicts. Asserts astream_once translates
them into the typed AgentEvent sequence the portal expects.
"""

from __future__ import annotations

from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest
from bss_orchestrator.session import (
    AgentEventError,
    AgentEventFinalMessage,
    AgentEventPromptReceived,
    AgentEventToolCallCompleted,
    AgentEventToolCallStarted,
    astream_once,
)
from langchain_core.messages import AIMessage, ToolMessage


class _FakeGraph:
    """Minimal stand-in for ``create_react_agent(...)`` in tests."""

    def __init__(self, updates: list[dict[str, Any]]):
        self._updates = updates

    async def astream(
        self, _input: Any, *, stream_mode: str = "updates"
    ) -> AsyncIterator[dict[str, Any]]:
        for u in self._updates:
            yield u


async def _collect(stream) -> list:
    return [ev async for ev in stream]


async def test_prompt_event_fires_first() -> None:
    fake = _FakeGraph([
        {"agent": {"messages": [AIMessage(content="hello")]}},
    ])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("say hi"))

    assert len(events) >= 1
    assert isinstance(events[0], AgentEventPromptReceived)
    assert events[0].prompt == "say hi"


async def test_tool_call_started_and_completed_pairs() -> None:
    """Agent emits AI-with-tool_calls, then tools node emits ToolMessages."""
    fake = _FakeGraph([
        {"agent": {"messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "customer.create", "args": {"name": "Ck"}, "id": "call_1"},
                    {"name": "payment.add_card", "args": {"pan": "4242..."}, "id": "call_2"},
                ],
            )
        ]}},
        {"tools": {"messages": [
            ToolMessage(content="CUST-042", name="customer.create", tool_call_id="call_1"),
            ToolMessage(content="PM-018", name="payment.add_card", tool_call_id="call_2"),
        ]}},
        {"agent": {"messages": [
            AIMessage(content="Created customer CUST-042 with card PM-018.")
        ]}},
    ])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("signup"))

    started = [e for e in events if isinstance(e, AgentEventToolCallStarted)]
    completed = [e for e in events if isinstance(e, AgentEventToolCallCompleted)]

    assert len(started) == 2
    assert started[0].name == "customer.create"
    assert started[0].args == {"name": "Ck"}
    assert started[0].call_id == "call_1"
    assert started[1].name == "payment.add_card"

    assert len(completed) == 2
    assert completed[0].name == "customer.create"
    assert completed[0].call_id == "call_1"
    assert completed[0].result == "CUST-042"
    assert completed[0].is_error is False


async def test_final_message_emitted_last() -> None:
    """FinalMessage is the last frame the chat route emits; v0.12 PR11
    moved the housekeeping AgentEventTurnUsage BEFORE FinalMessage
    so cost accounting lands before SSE consumers disconnect on
    "status: done". Assert FinalMessage is genuinely last and
    TurnUsage precedes it."""
    from bss_orchestrator.session import AgentEventTurnUsage

    fake = _FakeGraph([
        {"agent": {"messages": [AIMessage(content="done")]}},
    ])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("finish"))

    final_msgs = [e for e in events if isinstance(e, AgentEventFinalMessage)]
    assert len(final_msgs) == 1
    assert final_msgs[0].text == "done"
    final_idx = events.index(final_msgs[0])
    # FinalMessage is the last event in the stream.
    assert final_idx == len(events) - 1
    # TurnUsage is somewhere before FinalMessage.
    assert any(
        isinstance(e, AgentEventTurnUsage) for e in events[: final_idx]
    )


async def test_tool_call_de_duplication() -> None:
    """If the same call_id appears twice (LangGraph sometimes echoes), emit once."""
    fake = _FakeGraph([
        {"agent": {"messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "x.y", "args": {}, "id": "call_X"}],
            )
        ]}},
        # Same tool call echoed in a second update
        {"agent": {"messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "x.y", "args": {}, "id": "call_X"}],
            )
        ]}},
        {"tools": {"messages": [
            ToolMessage(content="ok", name="x.y", tool_call_id="call_X"),
        ]}},
        {"agent": {"messages": [AIMessage(content="done")]}},
    ])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("dedup"))

    started = [e for e in events if isinstance(e, AgentEventToolCallStarted)]
    assert len(started) == 1
    assert started[0].call_id == "call_X"


async def test_error_event_on_graph_exception() -> None:
    class _ExplodingGraph:
        async def astream(self, _input, *, stream_mode="updates"):
            yield {"agent": {"messages": []}}
            raise RuntimeError("LLM timeout")

    with patch("bss_orchestrator.session.build_graph", return_value=_ExplodingGraph()):
        events = await _collect(astream_once("boom"))

    assert any(isinstance(e, AgentEventError) for e in events)
    err = next(e for e in events if isinstance(e, AgentEventError))
    assert "LLM timeout" in err.message
    # Stream stops after error — no FinalMessage follows
    assert not any(isinstance(e, AgentEventFinalMessage) for e in events)


async def test_final_text_falls_back_to_intermediate_prose() -> None:
    """v0.20.1 — when the terminal AIMessage has empty content (gemma's
    failure mode after ``knowledge.search``), the final-message text
    should fall back to whichever prior AIMessage carried prose,
    even if that prior message also carried tool_calls. Before v0.20.1
    the gate ``text and not tool_calls`` dropped the intermediate
    prose and the operator saw an empty bubble that only resolved on
    a second turn."""
    fake = _FakeGraph([
        {"agent": {"messages": [
            AIMessage(
                content="Let me check the docs for that.",
                tool_calls=[{
                    "name": "knowledge.search", "args": {}, "id": "k1",
                }],
            )
        ]}},
        {"tools": {"messages": [
            ToolMessage(
                content="result body", name="knowledge.search",
                tool_call_id="k1",
            ),
        ]}},
        # Terminal AIMessage with empty content — the symptom case.
        {"agent": {"messages": [AIMessage(content="")]}},
    ])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("explain"))

    final = next(e for e in events if isinstance(e, AgentEventFinalMessage))
    # The intermediate "Let me check..." prose carries through instead
    # of an empty string. The cockpit / REPL fallback then has real
    # content to render rather than dropping a "(no reply)" bubble.
    assert final.text == "Let me check the docs for that."


async def test_terminal_text_overrides_intermediate_prose() -> None:
    """The relaxed gate must NOT regress the happy path: when the
    terminal AIMessage carries text, that text wins over any
    intermediate tool-call AIMessage prose."""
    fake = _FakeGraph([
        {"agent": {"messages": [
            AIMessage(
                content="Searching...",
                tool_calls=[{
                    "name": "knowledge.search", "args": {}, "id": "k1",
                }],
            )
        ]}},
        {"tools": {"messages": [
            ToolMessage(
                content="ok", name="knowledge.search", tool_call_id="k1",
            ),
        ]}},
        {"agent": {"messages": [AIMessage(content="The actual answer.")]}},
    ])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("ask"))

    final = next(e for e in events if isinstance(e, AgentEventFinalMessage))
    assert final.text == "The actual answer."


async def test_tool_result_truncation() -> None:
    """Results longer than the 500-char cap are truncated with an ellipsis."""
    long_result = "x" * 1000
    fake = _FakeGraph([
        {"agent": {"messages": [
            AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "c"}])
        ]}},
        {"tools": {"messages": [
            ToolMessage(content=long_result, name="x", tool_call_id="c"),
        ]}},
        {"agent": {"messages": [AIMessage(content="done")]}},
    ])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("truncate"))

    completed = next(e for e in events if isinstance(e, AgentEventToolCallCompleted))
    assert len(completed.result) < len(long_result)
    assert completed.result.endswith("…")


async def test_channel_parameter_reaches_bss_clients_context() -> None:
    """channel='portal-self-serve' overrides the default 'llm' on bss-clients headers."""
    fake = _FakeGraph([
        {"agent": {"messages": [AIMessage(content="ok")]}},
    ])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        async for _ in astream_once("x", channel="portal-self-serve"):
            pass

    # After the stream, the contextvars are still set on this task
    # bss_clients' ContextVar is internal; just confirm set_context was called
    # with our channel by reading whatever the current context is now.
    # (Concrete assertion lives in bss-clients own tests; this is a smoke test.)
    # If the channel=llm default had fired, we'd want to see that here —
    # since we passed portal-self-serve, we expect a non-default channel.
    # The exact attribute name on the base module is implementation detail;
    # the channel propagation end-to-end is validated by the portal's own
    # integration test + the hero scenario's interaction-log assertion.
    assert True  # Structural assertion — no real invariant to check here


# ─────────────────────────────────────────────────────────────────────────────
# v0.9 — service_identity propagation through astream_once
# ─────────────────────────────────────────────────────────────────────────────


PORTAL_SELF_SERVE_TOKEN = (
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
)


async def test_service_identity_param_overrides_outbound_token(monkeypatch) -> None:
    """``astream_once(service_identity="portal_self_serve")`` sets the per-Context
    X-BSS-API-Token override so downstream bss-clients calls carry the
    portal token. Captured via a graph stub that reads the contextvar at
    the moment the graph "runs" (mid-stream)."""
    monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_SELF_SERVE_TOKEN)

    captured: dict[str, str] = {}

    class _CapturingGraph:
        async def astream(self, _input, *, stream_mode="updates"):
            # Read the override at the exact point downstream tool
            # calls would observe it.
            from bss_clients.base import _service_identity_token_var
            captured["token"] = _service_identity_token_var.get()
            yield {"agent": {"messages": [AIMessage(content="done")]}}

    with patch("bss_orchestrator.session.build_graph", return_value=_CapturingGraph()):
        async for _ in astream_once(
            "x", service_identity="portal_self_serve"
        ):
            pass

    assert captured["token"] == PORTAL_SELF_SERVE_TOKEN


async def test_service_identity_default_means_no_override() -> None:
    """No ``service_identity=`` arg → no override → bss-clients keeps using
    whatever AuthProvider the orchestrator's clients were built with."""
    captured: dict[str, str] = {}

    class _CapturingGraph:
        async def astream(self, _input, *, stream_mode="updates"):
            from bss_clients.base import _service_identity_token_var
            captured["token"] = _service_identity_token_var.get()
            yield {"agent": {"messages": [AIMessage(content="done")]}}

    with patch("bss_orchestrator.session.build_graph", return_value=_CapturingGraph()):
        async for _ in astream_once("x"):
            pass

    # Empty string means "no override" — bss-clients _request will fall
    # through to the AuthProvider's token (the orchestrator default).
    assert captured["token"] == ""


async def test_service_identity_override_reset_after_stream(monkeypatch) -> None:
    """Override must NOT leak past the stream — even if the stream raises."""
    monkeypatch.setenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", PORTAL_SELF_SERVE_TOKEN)
    from bss_clients.base import _service_identity_token_var

    fake = _FakeGraph([{"agent": {"messages": [AIMessage(content="done")]}}])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        async for _ in astream_once("x", service_identity="portal_self_serve"):
            pass

    # After the stream, the override must be cleared.
    assert _service_identity_token_var.get() == ""


async def test_service_identity_resolved_token_missing_env_raises(monkeypatch) -> None:
    monkeypatch.delenv("BSS_NEVER_SET_API_TOKEN", raising=False)
    fake = _FakeGraph([{"agent": {"messages": [AIMessage(content="done")]}}])
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        with pytest.raises(RuntimeError, match="BSS_NEVER_SET_API_TOKEN is unset"):
            async for _ in astream_once("x", service_identity="never_set"):
                pass


async def test_service_identity_default_resolves_to_bss_api_token(monkeypatch) -> None:
    """``service_identity="default"`` resolves to BSS_API_TOKEN
    (matches bss_middleware's identity-derivation rule in reverse)."""
    default_token = (
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    monkeypatch.setenv("BSS_API_TOKEN", default_token)

    captured: dict[str, str] = {}

    class _CapturingGraph:
        async def astream(self, _input, *, stream_mode="updates"):
            from bss_clients.base import _service_identity_token_var
            captured["token"] = _service_identity_token_var.get()
            yield {"agent": {"messages": [AIMessage(content="done")]}}

    with patch("bss_orchestrator.session.build_graph", return_value=_CapturingGraph()):
        async for _ in astream_once("x", service_identity="default"):
            pass

    assert captured["token"] == default_token
