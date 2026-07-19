"""astream_once chat-scoping wiring (v0.12 PR2).

Asserts the new ``tool_filter`` + ``system_prompt`` + ``actor`` parameters
on ``astream_once`` propagate to ``build_graph`` and that the
orchestrator-side auth_context is set for the duration of the stream.
"""

from __future__ import annotations

from typing import Any, AsyncIterator
from unittest.mock import patch

from bss_orchestrator import auth_context
from bss_orchestrator.session import astream_once
from langchain_core.messages import AIMessage


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


async def test_tool_filter_passed_to_build_graph() -> None:
    fake = _FakeGraph([{"agent": {"messages": [AIMessage(content="hi")]}}])
    captured: dict[str, Any] = {}

    def _fake_build_graph(**kwargs: Any) -> _FakeGraph:
        captured.update(kwargs)
        return fake

    with patch("bss_orchestrator.session.build_graph", side_effect=_fake_build_graph):
        await _collect(
            astream_once(
                "ping",
                channel="portal-self-serve",
                actor="CUST-042",
                tool_filter="customer_self_serve",
            )
        )

    assert captured.get("tool_filter") == "customer_self_serve"


async def test_system_prompt_passed_to_build_graph() -> None:
    fake = _FakeGraph([{"agent": {"messages": [AIMessage(content="hi")]}}])
    captured: dict[str, Any] = {}

    def _fake_build_graph(**kwargs: Any) -> _FakeGraph:
        captured.update(kwargs)
        return fake

    with patch("bss_orchestrator.session.build_graph", side_effect=_fake_build_graph):
        await _collect(
            astream_once(
                "ping",
                actor="CUST-042",
                system_prompt="You are the customer-chat assistant for CUST-042.",
            )
        )

    assert captured.get("system_prompt") == (
        "You are the customer-chat assistant for CUST-042."
    )


async def test_auth_context_actor_set_during_stream_and_reset_after() -> None:
    seen_actors: list[str | None] = []

    class _ProbeGraph(_FakeGraph):
        async def astream(self, _input, *, stream_mode="updates"):  # type: ignore[no-untyped-def]
            seen_actors.append(auth_context.current().actor)
            for u in self._updates:
                yield u

    fake = _ProbeGraph([{"agent": {"messages": [AIMessage(content="hi")]}}])

    # Before the stream the actor is None.
    assert auth_context.current().actor is None
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        await _collect(astream_once("ping", actor="CUST-042"))

    # Inside the graph's astream the actor was bound.
    assert seen_actors == ["CUST-042"]
    # After the stream completes (or raises) the actor is cleared.
    assert auth_context.current().actor is None


async def test_auth_context_resets_even_on_error() -> None:
    class _BoomGraph(_FakeGraph):
        async def astream(self, _input, *, stream_mode="updates"):  # type: ignore[no-untyped-def]
            raise RuntimeError("graph blew up mid-stream")
            yield  # pragma: no cover — make this an async generator

    with patch("bss_orchestrator.session.build_graph", return_value=_BoomGraph([])):
        events = await _collect(astream_once("ping", actor="CUST-042"))

    # The astream consumed the error into an AgentEventError instead
    # of leaking the exception. Critically, the actor must still be
    # cleared from auth_context — finally-block discipline.
    assert auth_context.current().actor is None
    assert any(getattr(e, "message", "").startswith("RuntimeError") for e in events)


async def test_actor_omitted_keeps_auth_context_default() -> None:
    fake = _FakeGraph([{"agent": {"messages": [AIMessage(content="hi")]}}])

    assert auth_context.current().actor is None
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        await _collect(astream_once("ping"))
    # actor=None means the wrappers would refuse — confirms the seam
    # never silently inherits a stale actor from a prior stream.
    assert auth_context.current().actor is None


async def test_turn_usage_event_emitted_at_stream_end() -> None:
    """v0.12 PR5 — the chat route reads AgentEventTurnUsage to call
    chat_caps.record_chat_turn with the right token counts."""
    from bss_orchestrator.session import AgentEventTurnUsage

    final_msg = AIMessage(
        content="done",
        usage_metadata={
            "input_tokens": 1234,
            "output_tokens": 567,
            "total_tokens": 1801,
        },
        response_metadata={"model_name": "google/gemma-4-26b-a4b-it"},
    )
    fake = _FakeGraph([{"agent": {"messages": [final_msg]}}])

    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("ping"))

    usage = [e for e in events if isinstance(e, AgentEventTurnUsage)]
    assert len(usage) == 1
    assert usage[0].prompt_tok == 1234
    assert usage[0].completion_tok == 567
    assert usage[0].model == "google/gemma-4-26b-a4b-it"


async def test_turn_usage_event_emits_zero_when_no_metadata() -> None:
    """No usage_metadata → still emit a TurnUsage event with zeros so
    the route can rely on the event being present without a None-check."""
    from bss_orchestrator.session import AgentEventTurnUsage

    fake = _FakeGraph(
        [{"agent": {"messages": [AIMessage(content="done")]}}]
    )
    with patch("bss_orchestrator.session.build_graph", return_value=fake):
        events = await _collect(astream_once("ping"))

    usage = [e for e in events if isinstance(e, AgentEventTurnUsage)]
    assert len(usage) == 1
    assert usage[0].prompt_tok == 0
    assert usage[0].completion_tok == 0
