"""LLM-mode step executor — runs ``ask:`` steps through the LangGraph orchestrator.

One ``ask:`` step = one turn of the OpenRouter-backed supervisor graph.
We set the ``llm`` channel context so every downstream bss-clients call
carries ``X-BSS-Channel: llm`` + the model-derived actor (this is what
the final `interaction.list` assertion in scenario 3 relies on).

Extraction pattern from ``state["messages"]``:

* ``AIMessage``  — the assistant turns. The last one is the natural-language
  reply we report back. Its ``tool_calls`` attribute (when present) names the
  tools the model *asked* to invoke in THAT turn.
* ``ToolMessage`` — one per tool invocation that actually ran. Carries ``.name``
  (the registered tool name) and ``.content`` (the JSON result as a string).

We count tool invocations from ``ToolMessage`` nodes — that is the canonical
"did the tool run?" signal. ``AIMessage.tool_calls`` is what the model
*intended* and can include calls the graph refused (e.g., destructive-gated).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from .context import ScenarioContext
from .schema import AskStep


class LLMDisabled(RuntimeError):
    """Raised when an ``ask:`` step runs with ``--no-llm``."""


@dataclass
class LLMStepOutcome:
    """Everything an ``ask:`` step produces that expectations evaluate against."""

    final_message: str = ""
    tools_called: list[str] = field(default_factory=list)
    events_emitted: list[str] = field(default_factory=list)
    tokens_used: int = 0
    cost_usd: float = 0.0
    final_state_probe: dict[str, Any] = field(default_factory=dict)
    messages: list[BaseMessage] = field(default_factory=list)


def _last_ai_text(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, str):
                return content
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
    return ""


def _tool_names_called(messages: list[BaseMessage]) -> list[str]:
    """Return, in call order, every tool name that actually executed."""
    return [m.name for m in messages if isinstance(m, ToolMessage) and m.name]


async def execute_ask_step(
    step: AskStep,
    context: ScenarioContext,
    *,
    allow_llm: bool,
) -> LLMStepOutcome:
    """Run an ``ask:`` step and return the outcome.

    Raises :class:`LLMDisabled` when ``allow_llm`` is False (``--no-llm``).
    """
    if not allow_llm:
        raise LLMDisabled(
            f"ask: step {step.name!r} cannot run with --no-llm. "
            "Re-run without --no-llm or convert to an action: step."
        )

    # Deferred imports — keep the deterministic (--no-llm) path free of
    # LangGraph / OpenRouter dependencies.
    from bss_orchestrator.context import use_llm_context
    from bss_orchestrator.graph import build_graph

    use_llm_context()
    graph = build_graph(allow_destructive=False)

    prompt = context.interpolate(step.ask)
    # recursion_limit caps LLM → tool → LLM cycles; a blocked-subscription
    # troubleshooter shouldn't need anywhere near 40 turns, and hitting the
    # cap surfaces the "looping" failure mode cleanly instead of waiting for
    # the outer asyncio timeout.
    coro = graph.ainvoke(
        {"messages": [HumanMessage(content=prompt)]},
        config={"recursion_limit": 40},
    )
    try:
        state = await asyncio.wait_for(coro, timeout=step.timeout_seconds)
    except asyncio.TimeoutError as e:
        raise RuntimeError(
            f"LLM turn timed out after {step.timeout_seconds}s "
            f"(step {step.name!r})"
        ) from e

    messages: list[BaseMessage] = list(state.get("messages", []))
    return LLMStepOutcome(
        final_message=_last_ai_text(messages),
        tools_called=_tool_names_called(messages),
        messages=messages,
    )
