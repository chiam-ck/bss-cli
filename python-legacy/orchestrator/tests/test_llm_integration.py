"""Real-network smoke test against OpenRouter.

Marked ``@pytest.mark.integration`` — excluded from ``make test`` by default.
Run explicitly with:

    uv run --package bss-orchestrator pytest orchestrator/tests/test_llm_integration.py -m integration

Requires ``BSS_LLM_API_KEY`` to be set (OpenRouter key). If absent, the test
is skipped rather than failed — local CI runs without credentials.

Scope: **prove the wiring, not the model.** We ask the LLM a trivial
question that exercises one tool call (``clock.now``), and assert:

1. The LLM actually produced an ``AIMessage`` (answer came back).
2. At least one tool invocation occurred.
3. No auth/config error was raised.

We intentionally do NOT assert the final text wording — small models
phrase things differently on every run.

v0.13 — uses ``astream_once`` directly (the in-memory ``Session``
class was retired; multi-turn now goes through the cockpit's
``Conversation`` store + ``astream_once(transcript=...)``).
"""

from __future__ import annotations

import os

import pytest
from bss_orchestrator.config import settings
from bss_orchestrator.session import (
    AgentEventFinalMessage,
    AgentEventToolCallCompleted,
    astream_once,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def _require_api_key() -> None:
    if not (settings.llm_api_key or os.environ.get("BSS_LLM_API_KEY")):
        pytest.skip("BSS_LLM_API_KEY unset — skipping real-network smoke test.")


async def test_single_turn_invokes_clock_tool(_require_api_key: None) -> None:
    """One turn that should prompt the LLM to call ``clock.now``."""
    final_text = ""
    tool_calls_completed: list[AgentEventToolCallCompleted] = []

    async for event in astream_once(
        "What is the current system time? Use the clock tool to find out."
    ):
        if isinstance(event, AgentEventToolCallCompleted):
            tool_calls_completed.append(event)
        elif isinstance(event, AgentEventFinalMessage):
            final_text = event.text

    assert isinstance(final_text, str)
    assert final_text.strip(), "LLM returned empty reply"
    assert tool_calls_completed, (
        "No tool invocations observed — the LLM answered purely from priors"
    )
