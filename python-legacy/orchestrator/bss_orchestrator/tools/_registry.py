"""Tool registry — single chokepoint that collects every LLM-callable function.

Each tool module imports ``register`` and decorates its async functions. The
graph (``bss_orchestrator/graph.py``) walks ``TOOL_REGISTRY`` at build time
and wraps each function as a LangChain ``StructuredTool`` with the supplied
dotted name (``subscription.purchase_vas``) — matching the dotted names used
by ``DESTRUCTIVE_TOOLS`` in ``safety.py`` and by ``TOOL_SURFACE.md``.

Tools stay *dumb*: no retries, no fallbacks, no business logic. The
orchestrator supervisor handles retries and planning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

AsyncTool = Callable[..., Awaitable[Any]]

TOOL_REGISTRY: dict[str, AsyncTool] = {}


def register(name: str) -> Callable[[AsyncTool], AsyncTool]:
    """Register ``fn`` under the dotted LLM tool name (e.g. ``customer.create``).

    Raises at import time if two tools try to claim the same name so drift
    between ``TOOL_SURFACE.md`` and the registry is caught early.
    """

    def _deco(fn: AsyncTool) -> AsyncTool:
        if name in TOOL_REGISTRY:
            raise RuntimeError(f"Duplicate tool registration: {name!r}")
        fn.__tool_name__ = name  # type: ignore[attr-defined]
        TOOL_REGISTRY[name] = fn
        return fn

    return _deco


def get_tool(name: str) -> AsyncTool:
    """Fetch a registered tool by its dotted name, or raise ``KeyError``."""
    try:
        return TOOL_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown tool: {name!r}") from exc


def list_tools() -> list[str]:
    """Return all registered tool names, sorted for stable display."""
    return sorted(TOOL_REGISTRY)
