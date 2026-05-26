"""LangGraph supervisor graph — binds TOOL_REGISTRY + safety gating + LLM.

Call ``build_graph(allow_destructive=...)`` to get a compiled agent. The
returned object has the ``ainvoke``/``astream`` surface from
``langgraph.prebuilt.create_react_agent``.

Design notes:
- Tools stay dumb. All retry/planning behaviour lives in the ReAct loop.
- Each Python async function in ``TOOL_REGISTRY`` is wrapped by
  ``wrap_destructive`` then registered as a ``StructuredTool`` with the
  dotted name the LLM sees (``subscription.purchase_vas``).
- Descriptions come straight from the function docstring — the semantic
  contract the tests enforce.
- Arg schemas are inferred by LangChain from the coroutine signature, so the
  ``Annotated[str, "format hint"]`` metadata in ``types.py`` flows into the
  JSON Schema the model sees. That is the whole point of the semantic layer.
"""

from __future__ import annotations

import functools
from typing import Any

from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent

from .llm import build_chat_model
from .prompts import SYSTEM_PROMPT
from .safety import LoopState, make_loop_state, wrap_destructive
from .tools import TOOL_PROFILES, TOOL_REGISTRY

# Tools present in TOOL_REGISTRY (so scenarios can use them via ``action:``)
# but intentionally NOT exposed to the LLM. The LLM gets a model-visible
# subset — we pull out scenario-scaffolding tools that small models tend
# to misuse during troubleshooting (e.g. burning allowance to "test" a fix).
_LLM_HIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        # Injects real usage events. Legit from a test harness or channel
        # layer; never from the copilot — read-only ``usage.history`` and
        # ``subscription.get`` are the right troubleshooting surfaces.
        "usage.simulate",
        # v0.7 — catalog admin writes are CLI/scenario-only. The LLM tool
        # surface deliberately omits them so the model never edits the
        # catalog mid-conversation.
        "catalog.add_offering",
        "catalog.add_price",
        "catalog.window_offering",
        # v0.7 — operator price migration carries a notice period and is
        # an explicit operator action. CLI/scenarios only.
        "subscription.migrate_to_new_price",
    }
)


def _tool_error_to_observation(exc: Exception) -> str:
    """Convert ANY exception from a tool call into an LLM-readable observation.

    PolicyViolationFromServer and ClientError carry structured ``rule`` /
    ``status_code`` + ``detail`` fields — expose them so the model can read
    the failure and recover (retry with corrections, ask the user, etc.)
    rather than watching the graph crash.
    """
    from bss_clients.errors import ClientError, PolicyViolationFromServer

    if isinstance(exc, PolicyViolationFromServer):
        return f'{{"error": "POLICY_VIOLATION", "rule": "{exc.rule}", "detail": {exc.detail!r}}}'
    if isinstance(exc, ClientError):
        return f'{{"error": "CLIENT_ERROR", "status": {exc.status_code}, "detail": {exc.detail!r}}}'
    return f'{{"error": "{type(exc).__name__}", "detail": "{exc}"}}'


def _as_structured_tool(
    name: str,
    fn: Any,
    *,
    allow_destructive: bool,
    autonomy_mode: str,
    loop_state: LoopState,
) -> StructuredTool:
    """Wrap a registered async tool as a LangChain ``StructuredTool``.

    We wrap the coroutine in a try/except that converts ANY exception to a
    string observation. LangChain's ``handle_tool_error`` only fires for
    ``ToolException``, and wrapping inside the coroutine means the graph
    never sees the exception at all — the tool simply returns an
    error-shaped string and the ReAct loop reads it as a normal observation.

    The shared ``loop_state`` is what lets granular mode re-gate after the
    first destructive tool fires — every wrapper in this graph reads
    the same dict.
    """
    gated = wrap_destructive(
        fn,
        tool_name=name,
        allow_destructive=allow_destructive,
        autonomy_mode=autonomy_mode,
        loop_state=loop_state,
    )
    description = (fn.__doc__ or "").strip() or f"BSS tool {name}."

    # ``functools.wraps(fn)`` copies ``__wrapped__`` (among other dunders) so
    # ``inspect.signature`` — which LangChain uses to infer ``args_schema`` —
    # resolves back to ``fn``'s real signature with its ``Annotated[...]``
    # type hints. We need that so the JSON Schema the model sees matches
    # ``types.py``, not our generic ``**kwargs`` catch-all.
    @functools.wraps(fn)
    async def _safe(**kwargs: Any) -> Any:
        try:
            return await gated(**kwargs)
        except Exception as exc:
            return _tool_error_to_observation(exc)

    return StructuredTool.from_function(
        coroutine=_safe,
        name=name,
        description=description,
    )


def build_tools(
    *,
    allow_destructive: bool = False,
    tool_filter: str | None = None,
    autonomy_mode: str = "batched",
) -> list[StructuredTool]:
    """Return the LLM-visible tool list, safety-wrapped.

    Args:
        allow_destructive: see ``build_graph``.
        tool_filter: profile name from ``TOOL_PROFILES`` (e.g.
            ``"customer_self_serve"``). When set, the returned list is
            the intersection of that profile and the registered tools;
            ``_LLM_HIDDEN_TOOLS`` still applies. When ``None``, every
            registered, non-hidden tool is returned (CLI / scenario /
            CSR behaviour).
        autonomy_mode: ``"granular"`` re-gates each destructive after
            the first fires; ``"batched"`` (default for backward-compat
            with non-cockpit callers) authorises the whole loop after
            the first destructive runs. v1.5 cockpit callers pass the
            value from ``read_autonomy_mode()``.

    Raises:
        KeyError: ``tool_filter`` names a profile that does not exist.
    """
    allowed: set[str] | None
    if tool_filter is None:
        allowed = None
    else:
        allowed = TOOL_PROFILES[tool_filter]
    # One LoopState per graph build; shared across every wrapped
    # destructive tool so granular mode can observe the first-fire
    # signal consistently.
    loop_state = make_loop_state()
    return [
        _as_structured_tool(
            name,
            fn,
            allow_destructive=allow_destructive,
            autonomy_mode=autonomy_mode,
            loop_state=loop_state,
        )
        for name, fn in sorted(TOOL_REGISTRY.items())
        if name not in _LLM_HIDDEN_TOOLS
        and (allowed is None or name in allowed)
    ]


def build_graph(
    *,
    allow_destructive: bool = False,
    temperature: float = 0.0,
    tool_filter: str | None = None,
    system_prompt: str | None = None,
    autonomy_mode: str = "batched",
) -> Any:
    """Compile a ReAct agent over the BSS tool surface.

    Args:
        allow_destructive: If ``False`` (default) every destructive tool call
            short-circuits with a structured ``DESTRUCTIVE_OPERATION_BLOCKED``
            result and the LLM sees that and explains the situation to the
            user. Set to ``True`` only when the human has passed
            ``--allow-destructive`` (CLI) or after ``/confirm`` (cockpit).
        temperature: LLM sampling temperature. Default ``0.0``.
        tool_filter: optional ``TOOL_PROFILES`` key — narrows the
            LLM-visible tool list to one curated profile (v0.12 chat
            scoping). ``None`` keeps the full surface.
        system_prompt: optional override for the agent's system
            message. Defaults to the canonical ``SYSTEM_PROMPT``;
            v0.12 chat passes the customer-chat prompt instead.
        autonomy_mode: v1.5 — ``"granular"`` re-gates each destructive
            after the first fires; ``"batched"`` authorises the loop.
            Defaults to ``"batched"`` to preserve pre-v1.5 behaviour
            for callers (scenarios, tests) that haven't been updated.
            Production cockpit callers in v1.5+ pass the value cached
            from ``read_autonomy_mode()`` at process boot.

    Returns:
        A compiled LangGraph runnable. Invoke with
        ``{"messages": [("user", text)]}`` → receive updated messages.
    """
    llm = build_chat_model(temperature=temperature)
    tools = build_tools(
        allow_destructive=allow_destructive,
        tool_filter=tool_filter,
        autonomy_mode=autonomy_mode,
    )
    return create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt or SYSTEM_PROMPT,
    )
