"""Graph wiring tests — StructuredTool conversion + safety gating composition.

We don't instantiate the LLM here (that needs an API key + network). We just
verify that ``build_tools`` converts every registry entry into a properly
named StructuredTool, and that destructive gating short-circuits correctly
at the StructuredTool boundary.
"""

from __future__ import annotations

import pytest
from bss_orchestrator.graph import _LLM_HIDDEN_TOOLS, build_tools
from bss_orchestrator.safety import DESTRUCTIVE_TOOLS
from bss_orchestrator.tools import TOOL_REGISTRY


def test_build_tools_matches_registry_size() -> None:
    tools = build_tools(allow_destructive=False)
    # Every registered tool except those explicitly hidden from the LLM
    # (scenario-scaffolding tools like ``usage.simulate``).
    assert len(tools) == len(TOOL_REGISTRY) - len(_LLM_HIDDEN_TOOLS)


def test_build_tools_preserves_dotted_names() -> None:
    tools = build_tools(allow_destructive=False)
    names = {t.name for t in tools}
    assert names == set(TOOL_REGISTRY) - _LLM_HIDDEN_TOOLS


def test_hidden_tools_are_actually_registered() -> None:
    # If a name lands in _LLM_HIDDEN_TOOLS but not the registry, the hide
    # filter silently no-ops and a future regression is invisible.
    missing = _LLM_HIDDEN_TOOLS - set(TOOL_REGISTRY)
    assert not missing, f"hidden-from-LLM tools are not registered: {missing}"


def test_build_tools_descriptions_come_from_docstrings() -> None:
    tools = build_tools(allow_destructive=False)
    by_name = {t.name: t for t in tools}
    # Pick a stable well-documented tool — subscription.get is always there.
    sub_get = by_name["subscription.get"]
    assert "subscription" in sub_get.description.lower()
    assert "balances" in sub_get.description.lower() or "state" in sub_get.description.lower()


@pytest.mark.asyncio
async def test_destructive_tool_blocks_without_flag() -> None:
    tools = build_tools(allow_destructive=False)
    by_name = {t.name: t for t in tools}
    terminate = by_name["subscription.terminate"]
    # coroutine invocation — StructuredTool.ainvoke runs the wrapped fn.
    result = await terminate.ainvoke({"subscription_id": "SUB-007"})
    assert isinstance(result, dict)
    assert result.get("error") == "DESTRUCTIVE_OPERATION_BLOCKED"
    assert result.get("tool") == "subscription.terminate"


def test_every_destructive_tool_in_surface_is_registered() -> None:
    # If a destructive tool lands in DESTRUCTIVE_TOOLS but isn't in the
    # registry, the gate silently never fires. Cross-check explicitly.
    registered = set(TOOL_REGISTRY)
    v0_1 = {t for t in DESTRUCTIVE_TOOLS if not t.startswith("admin.")}
    missing = v0_1 - registered
    assert not missing, f"destructive tools referenced but not registered: {missing}"


@pytest.mark.asyncio
async def test_clock_now_tool_actually_runs() -> None:
    # Regression for the F811 shadowing bug (2026-06-12): the tool def
    # ``clock_now`` shadowed ``from bss_clock import now as clock_now``,
    # so the body called ITSELF and every clock.now invocation raised
    # "'coroutine' object has no attribute 'replace'". The tool is
    # network-free, so invoke it for real — registration alone proved
    # nothing here.
    result = await TOOL_REGISTRY["clock.now"]()
    assert isinstance(result, dict)
    assert result["source"] == "system"
    # ISO-8601 with no microseconds — the docstring contract.
    assert "T" in result["now"] and "." not in result["now"]
