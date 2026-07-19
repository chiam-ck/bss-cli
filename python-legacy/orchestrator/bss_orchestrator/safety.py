"""Destructive-operation gating for LLM tool calls.

Policy: some tools can't be undone with a follow-up call (terminate a
subscription, remove a payment method, cancel an order). If the human hasn't
explicitly passed ``--allow-destructive``, these tools return a *structured*
error rather than executing:

    {"error": "DESTRUCTIVE_OPERATION_BLOCKED", "tool": "...", "message": "..."}

The LangGraph supervisor sees this structured error as the tool result and
either aborts cleanly or asks the user to re-run with the flag. Same pattern
as ``PolicyViolationFromServer`` — structured error, never a stack trace.

v1.5 — autonomy-aware gating. The base contract above is unchanged:
``allow_destructive=False`` always blocks. What v1.5 adds is differentiated
behaviour when ``allow_destructive=True``:

- ``autonomy_mode="batched"`` — the historical behaviour. Once allowed,
  every destructive tool call in the current graph instance executes.
  The cockpit's per-turn ``allow_destructive=True`` flag thus authorises
  the whole loop.
- ``autonomy_mode="granular"`` (the new default) — only the FIRST destructive
  call in the current graph instance executes; subsequent destructive calls
  block again with the same structured error, so the LLM must re-propose
  and the operator must ``/confirm`` for each one. Per-step operator
  control even after the first authorisation.

The per-graph "first-destructive-fired" state lives in a ``LoopState``
dict that ``build_tools`` creates fresh and shares across every wrapped
destructive tool in the same ``build_graph`` invocation. Each ``astream_once``
call builds its own graph, so the state naturally resets between turns —
no manual reset required. Test paths that wrap a single tool directly
get an implicit private LoopState.

The destructive-tool list (``DESTRUCTIVE_TOOLS``) is unchanged in v1.5.
Autonomy controls *how many* ``/confirm``s a compound action needs, NOT
*which* tools require one. Adding a tool to ``DESTRUCTIVE_TOOLS`` is still
a doctrine decision that requires reviewing the safety contract.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

# Every destructive tool in the registry. Matches the dotted name the LLM sees
# (``<domain>.<action>``), NOT the Python function name — LangGraph tools are
# registered with the dotted name via ``@tool(name=...)``.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "customer.close",
        "customer.remove_contact_medium",
        "case.close",
        "ticket.cancel",
        "payment.remove_method",
        "order.cancel",
        "subscription.terminate",
        # v0.12 — chat-surface wrapper around subscription.terminate.
        # The wrapper narrows the LLM-visible target to the actor's
        # own line, but the operation is still irreversible — gating
        # at the wrapper too keeps the destructive contract honest
        # even if a future caller forgets allow_destructive=True.
        "subscription.terminate_mine",
        "provisioning.set_fault_injection",
        "admin.reset_operational_data",
        "admin.force_state",
    }
)


# v1.5 — per-graph-build mutable state shared across destructive wrappers.
# A plain dict keeps the contract obvious from the wrapper's call site.
# Single key today; reserving the dict shape so v1.5.x (per-tool autonomy
# annotations, per-session overrides) can extend it without a signature
# change ripple.
LoopState = dict[str, Any]


def make_loop_state() -> LoopState:
    """Return a fresh per-graph mutable state for autonomy-aware gating.

    Called once by ``build_tools`` per ``build_graph`` invocation; shared
    across every destructive wrapper in that graph so they observe a
    consistent "has any destructive fired in this loop yet?" answer.
    """
    return {"destructive_executed": 0}


def is_destructive(tool_name: str) -> bool:
    """True if ``tool_name`` (dotted, e.g. ``subscription.terminate``) is gated."""
    return tool_name in DESTRUCTIVE_TOOLS


def _blocked_response(tool_name: str) -> dict[str, Any]:
    return {
        "error": "DESTRUCTIVE_OPERATION_BLOCKED",
        "tool": tool_name,
        "message": (
            f"Tool {tool_name!r} is destructive and requires operator "
            "/confirm. Propose this tool to the operator by stopping after "
            "your proposal; the next /confirm-bracketed turn will execute "
            "it."
        ),
    }


def wrap_destructive(
    tool_fn: Callable[..., Awaitable[Any]],
    *,
    tool_name: str,
    allow_destructive: bool,
    autonomy_mode: str = "batched",
    loop_state: LoopState | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Return a coroutine wrapper that short-circuits if the tool is
    destructive and the gate is closed.

    Non-destructive tools are returned unchanged — no overhead.

    Args:
        tool_fn: the underlying tool coroutine.
        tool_name: dotted LLM-facing name (``"subscription.terminate"``).
        allow_destructive: master gate set by the caller (the CLI's
            ``--allow-destructive`` or the cockpit's per-turn
            ``allow_destructive=True`` after ``/confirm``).
        autonomy_mode: ``"granular"`` (re-gate after first destructive
            fires) or ``"batched"`` (one authorisation covers the
            loop). Defaults to ``"batched"`` to preserve pre-v1.5
            behaviour for callers that haven't been updated yet
            (mostly test paths); production call sites in v1.5+ pass
            the autonomy mode explicitly from
            ``read_autonomy_mode()`` cached on app/REPL state.
        loop_state: per-graph state created by ``make_loop_state()``;
            shared across every destructive wrapper in the same
            ``build_graph`` invocation. When ``None``, a private
            state is created (test-path convenience — production
            paths via ``build_tools`` always pass a shared state).
    """
    if not is_destructive(tool_name):
        return tool_fn

    state = loop_state if loop_state is not None else make_loop_state()

    @functools.wraps(tool_fn)
    async def _gated(**kwargs: Any) -> Any:
        if not allow_destructive:
            return _blocked_response(tool_name)
        # allow_destructive=True path.
        # Granular mode re-gates after the first destructive in this
        # graph has executed; batched mode authorises the whole loop.
        if (
            autonomy_mode == "granular"
            and state["destructive_executed"] >= 1
        ):
            return _blocked_response(tool_name)
        state["destructive_executed"] += 1
        return await tool_fn(**kwargs)

    return _gated
