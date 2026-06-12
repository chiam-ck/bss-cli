"""LLM session — single-shot + streaming entry points.

Two entry points (the v0.6 in-memory ``Session`` class was retired in
v0.13 — the cockpit's ``Conversation`` store is the only multi-turn
shape now, and ``astream_once(transcript=...)`` feeds prior turns into
the LangGraph messages list):

- ``ask_once(text, *, allow_destructive=False)`` — one-turn blocking call.
  Used by ``bss ask '…'``. No history.

- ``astream_once(text, *, allow_destructive=False, channel="llm",
  transcript="", ...)`` (v0.4+, transcript wired into graph messages
  in v0.13) — streaming variant of ask_once. Yields typed
  ``AgentEvent`` dataclasses as the graph produces them. Used by
  portals to render tool-call logs live via SSE. Same tool-chain as
  ask_once, same policy gating; just observable as it happens.

Both set the bss-clients context (channel header) before invoking the
graph so downstream service-to-service calls carry the right attribution.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Union

from bss_clients import reset_service_identity_token, set_service_identity_token
from bss_telemetry import semconv, tracer
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from . import auth_context
from .clients import get_clients
from .context import use_channel_context, use_llm_context
from .graph import build_graph
from .ownership import (
    AgentOwnershipViolation,
    assert_owned_output,
    record_violation,
)


# v1.5 — agentic loop bail when tool calls keep failing. Catches the
# Gemma thrash pattern where the model fires the same broken call over
# and over instead of adapting. Three consecutive failures = bail with
# a structured AgentEventError; any success resets the counter.
MAX_CONSECUTIVE_TOOL_FAILURES: int = 3

# Substring markers in the tool result that flag a "failure" for the
# purpose of the consecutive-failures counter. PolicyViolation +
# DESTRUCTIVE_OPERATION_BLOCKED + CLIENT_ERROR are the structured
# errors the orchestrator emits today; ToolMessage.status == "error"
# is the LangGraph-native signal for an actual exception. We treat
# all three the same — they all mean "the LLM's call didn't do what
# it intended", which is the thing that signals thrash.
_TOOL_FAILURE_MARKERS: tuple[str, ...] = (
    '"error": "DESTRUCTIVE_OPERATION_BLOCKED"',
    '"error": "POLICY_VIOLATION"',
    '"error": "CLIENT_ERROR"',
)


def _is_failure_tool_result(content: str, is_error: bool) -> bool:
    """True if the tool result should count as a failure for the bail
    counter. Conservative — false negatives are fine (we'd just bail
    later or not at all), but false positives could end a healthy
    investigation loop early."""
    if is_error:
        return True
    return any(marker in content for marker in _TOOL_FAILURE_MARKERS)


# v1.6.2 — stuck-loop bail, the success-shaped sibling of the failure
# counter above. The 2026-06-12 incident: asked to find a customer by
# email (no such tool existed then), gemma fired customer.list with the
# email as name_contains, got [] — a SUCCESS, so the failure counter
# reset — and re-fired the identical call. Three identical (tool, args)
# calls returning the identical result is degeneration, not
# investigation: the agent already has this answer and is asking again.
# Distinct results don't count — a poll whose target progresses
# (provisioning.get_task while a task advances) never trips.
MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS: int = 3


class _IdenticalCallTracker:
    """Count consecutive identical (tool, args, result) triples.

    ``record`` returns True when the run length reaches
    ``MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS``. Any differing call or
    differing result resets the run. One instance per stream — no
    cross-turn state.
    """

    def __init__(self) -> None:
        self._last_key: tuple[str, str, str] | None = None
        self._repeats = 0

    def record(self, name: str, args_sig: str, result: str) -> bool:
        key = (name, args_sig, result)
        if key == self._last_key:
            self._repeats += 1
        else:
            self._last_key = key
            self._repeats = 1
        return self._repeats >= MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS


def _tool_args_sig(args: Any) -> str:
    """Canonical signature for a tool call's args dict (key-order
    independent). ``default=str`` — unserialisable arg values must
    never break the stream over a diagnostic counter."""
    try:
        return json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(args)


def _resolve_token_for_service_identity(identity: str) -> str:
    """v0.9 — resolve a service_identity label to its env-loaded token.

    Mirrors the convention used by ``bss_middleware.api_token``:

    - ``"default"`` → ``BSS_API_TOKEN``
    - ``"<name>"`` → ``BSS_<NAME>_API_TOKEN`` (uppercased, underscored)

    Raises ``RuntimeError`` if the env var is unset. The orchestrator's
    own clients keep using ``BSS_API_TOKEN`` (via ``get_clients()``); the
    resolved token here is set into bss-clients' per-Context override
    so individual ``astream_once`` invocations can run "as another
    surface" for audit-attribution purposes (v0.11 portal chat).
    """
    if not identity:
        raise ValueError("service_identity must be a non-empty string")
    env_var = (
        "BSS_API_TOKEN" if identity == "default"
        else f"BSS_{identity.upper()}_API_TOKEN"
    )
    token = os.environ.get(env_var, "")
    if not token:
        raise RuntimeError(
            f"astream_once(service_identity={identity!r}): {env_var} is unset. "
            "The named token must be provisioned in the orchestrator's env "
            "for downstream calls to carry it. Generate via: openssl rand -hex 32"
        )
    return token


def _last_ai_text(messages: list[BaseMessage]) -> str:
    """Return the text of the final ``AIMessage`` in the turn, or empty."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, str):
                return content
            # some providers return list[{"type": "text", "text": ...}]
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Streaming event types (v0.4 — portal agent log widget)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentEventPromptReceived:
    """Emitted once at the start of the stream with the caller's raw prompt."""

    prompt: str


@dataclass(frozen=True)
class AgentEventToolCallStarted:
    """The LLM decided to invoke a tool. Emitted before the tool runs."""

    name: str
    args: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class AgentEventToolCallCompleted:
    """The tool's result came back.

    ``result`` is the truncated string repr — safe to flood into a
    log widget or SSE frame.

    ``result_full`` (v0.6+) is the untruncated string repr. Consumers
    that need to parse the JSON (e.g. the REPL's renderer-dispatch
    hook for ``*.get``-shaped tools) read this field. Defaults to
    the truncated value when full data wasn't captured (preserves
    backward compat for code that only used ``result``).
    """

    name: str
    call_id: str
    result: str
    is_error: bool = False
    result_full: str = ""


@dataclass(frozen=True)
class AgentEventFinalMessage:
    """Last AI message with no further tool calls — the end of the turn."""

    text: str


@dataclass(frozen=True)
class AgentEventError:
    """The graph or a tool raised an exception that escaped all handlers."""

    message: str


@dataclass(frozen=True)
class AgentEventTurnUsage:
    """v0.12 — token counts for the completed turn so the chat route
    can call ``chat_caps.record_chat_turn`` with the right numbers.

    Emitted once at stream end after ``AgentEventFinalMessage`` when
    the LLM's response surfaced ``usage_metadata``. ``model`` carries
    the model identifier used so the rate table looks up the right
    rates even if ``BSS_LLM_MODEL`` changes between turns.
    """

    prompt_tok: int
    completion_tok: int
    model: str


AgentEvent = Union[
    AgentEventPromptReceived,
    AgentEventToolCallStarted,
    AgentEventToolCallCompleted,
    AgentEventFinalMessage,
    AgentEventError,
    AgentEventTurnUsage,
]


_RESULT_TRUNCATE = 500


def _truncate(text: str, limit: int = _RESULT_TRUNCATE) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ─────────────────────────────────────────────────────────────────────────────
# ask_once — single-shot non-streaming entry point
# ─────────────────────────────────────────────────────────────────────────────
#
# v0.6's in-memory ``Session`` class was retired in v0.13. The cockpit's
# Postgres-backed ``Conversation`` store + ``astream_once(transcript=...)``
# is now the only multi-turn shape — process-local message lists no
# longer exist as a public surface.


async def ask_once(
    text: str,
    *,
    allow_destructive: bool = False,
    autonomy_mode: str = "batched",
) -> str:
    """Run a single question through a fresh graph — no session state kept.

    ``autonomy_mode`` defaults to ``"batched"`` for backward-compat with
    scenario/test callers that don't care about multi-step gating. The
    cockpit (which DOES care) calls ``astream_once`` with the cached
    mode from ``read_autonomy_mode()`` instead.
    """
    with tracer("bss-orchestrator").start_as_current_span("bss.ask") as span:
        span.set_attribute(semconv.BSS_CHANNEL, "llm")
        span.set_attribute("bss.ask.allow_destructive", allow_destructive)
        span.set_attribute("bss.ask.autonomy_mode", autonomy_mode)
        use_llm_context()
        graph = build_graph(
            allow_destructive=allow_destructive, autonomy_mode=autonomy_mode
        )
        state = await graph.ainvoke({"messages": [HumanMessage(content=text)]})
        return _last_ai_text(state["messages"])


# ─────────────────────────────────────────────────────────────────────────────
# astream_once — v0.4 streaming entry point
# ─────────────────────────────────────────────────────────────────────────────


async def astream_once(
    prompt: str,
    *,
    allow_destructive: bool = False,
    channel: str = "llm",
    actor: str | None = None,
    service_identity: str | None = None,
    tool_filter: str | None = None,
    system_prompt: str | None = None,
    transcript: str = "",
    autonomy_mode: str = "batched",
) -> AsyncIterator[AgentEvent]:
    """Streaming variant of ``ask_once``. Yields ``AgentEvent`` as the graph runs.

    The ``channel`` parameter overrides the X-BSS-Channel header on every
    outbound bss-clients call so CRM's interaction log can attribute the
    resulting actions to the right surface (v0.4 portal uses
    ``channel="portal-self-serve"``; v0.5 CSR uses ``channel="portal-csr"``).

    The ``actor`` parameter (v0.5+) sets X-BSS-Actor on outbound calls.
    The CSR portal passes the operator's id (``actor=<operator_id>``)
    so the interaction log attributes actions to the human who asked
    rather than to ``llm-<model-slug>``. Per-model attribution still
    lives in ``audit.domain_event.actor``. Defaults to ``settings.llm_actor``
    when ``channel != "llm"`` and no actor is given (preserves v0.4 behaviour).

    The ``tool_filter`` parameter (v0.12+) names a profile in
    ``TOOL_PROFILES`` (e.g. ``"customer_self_serve"``). When set, the
    LangGraph agent is constructed with the profile's tool subset
    instead of the full registry — the chat surface narrows the
    LLM-visible tools so an injected prompt cannot reach a tool the
    customer's own UI doesn't expose. ``None`` (default) keeps full
    access (CLI / scenario / CSR behaviour).

    The ``system_prompt`` parameter (v0.12+) overrides the canonical
    ``SYSTEM_PROMPT`` for this stream. v0.12 customer chat passes the
    customer-chat prompt with five-category escalation guidance.

    The ``service_identity`` parameter (v0.9+) overrides the X-BSS-API-Token
    on outbound bss-clients calls so audit rows attribute writes to the
    initiating surface (e.g. ``"portal_self_serve"`` when v0.11 portal chat
    routes a question through the orchestrator). The orchestrator's own
    clients are constructed with ``BSS_API_TOKEN``; this parameter
    populates a per-Context ``ContextVar`` that bss-clients reads on each
    request. The token is resolved by env-var convention (``"portal_self_serve"``
    → ``BSS_PORTAL_SELF_SERVE_API_TOKEN``); receiving services then resolve
    ``service_identity`` from token validation. Defaults to ``None`` —
    no override; callers using the orchestrator default identity get v0.4
    behaviour. v0.9 ships the parameter and propagation; the portal chat
    surface in v0.11 is the first caller.

    Event sequence:
    1. One ``AgentEventPromptReceived`` at the start.
    2. ``AgentEventToolCallStarted`` + ``AgentEventToolCallCompleted`` pairs
       as the LLM chains tool calls.
    3. One ``AgentEventFinalMessage`` when the agent stops calling tools.
    4. If anything raises past the graph's own error wrapping, one
       ``AgentEventError`` and the stream terminates.

    Contract note: tool observations are already converted to structured
    strings inside the graph (the try/except in ``_as_structured_tool`` —
    see DECISIONS.md 2026-04-12 Phase 10). This function observes those
    results and surfaces them as events; it does not add its own retry or
    recovery.
    """
    with tracer("bss-orchestrator").start_as_current_span("bss.ask") as span:
        span.set_attribute(semconv.BSS_CHANNEL, channel)
        span.set_attribute("bss.ask.allow_destructive", allow_destructive)
        span.set_attribute("bss.ask.autonomy_mode", autonomy_mode)
        span.set_attribute("bss.ask.streaming", True)
        if actor:
            span.set_attribute("bss.actor", actor)
        if service_identity:
            span.set_attribute("bss.service.identity", service_identity)

        if channel == "llm":
            use_llm_context()
        else:
            use_channel_context(channel=channel, actor=actor)

        # v0.9 — per-Context X-BSS-API-Token override. Resolves once
        # before the graph runs; reset on exit (in finally) so a stream
        # that raises still leaves the Context clean for whatever runs
        # next in the same task.
        identity_reset_token = None
        if service_identity:
            override_token = _resolve_token_for_service_identity(service_identity)
            identity_reset_token = set_service_identity_token(override_token)

        # v0.12 — bind the orchestrator-side auth_context for the
        # duration of this stream so the customer profile's *.mine
        # wrappers can read auth_context.current().actor. The
        # transcript carries the chat-session conversation history
        # for case.open_for_me's transcript-link when a customer
        # escalates. Reset in finally so a stream that raises still
        # leaves a clean Context for whatever runs next.
        auth_reset_token = None
        if actor:
            auth_reset_token = auth_context.set_actor(
                actor, transcript=transcript
            )

        try:
            yield AgentEventPromptReceived(prompt=prompt)

            graph = build_graph(
                allow_destructive=allow_destructive,
                tool_filter=tool_filter,
                system_prompt=system_prompt,
                autonomy_mode=autonomy_mode,
            )
            seen_call_ids: set[str] = set()
            last_ai_text = ""
            usage_total = {"input_tokens": 0, "output_tokens": 0, "model": ""}
            # v1.5 — bail counter (see MAX_CONSECUTIVE_TOOL_FAILURES).
            consecutive_failures = 0
            # v1.6.2 — stuck-loop bail (see _IdenticalCallTracker).
            # call_args_sigs pairs each ToolMessage back with the args
            # its AIMessage proposed — ToolMessage itself carries only
            # name + call_id.
            call_args_sigs: dict[str, str] = {}
            stuck_tracker = _IdenticalCallTracker()

            # v0.13 — when the caller passes a non-empty ``transcript``,
            # parse it into typed prior-turn messages and prepend them
            # to this turn's HumanMessage. Multi-turn coherence in the
            # LangGraph agent depends on the model seeing prior turns;
            # before v0.13, ``transcript`` was only consumed by
            # auth_context (for the v0.12 escalation transcript-link)
            # and the model saw a single isolated HumanMessage every
            # turn. The cockpit's Conversation.transcript_text() emits
            # the format ``role:\ncontent\n\nrole:\ncontent`` —
            # ``_messages_from_transcript`` is the parser. Empty
            # transcripts (chat surface pre-cockpit) yield ``[]`` so
            # the v0.12 contract is preserved.
            prior_messages: list[BaseMessage] = (
                _messages_from_transcript(transcript) if transcript else []
            )
            initial_messages: list[BaseMessage] = [
                *prior_messages,
                HumanMessage(content=prompt),
            ]

            try:
                async for update in graph.astream(
                    {"messages": initial_messages},
                    stream_mode="updates",
                ):
                    # update shape: {node_name: {"messages": [new_messages...]}}
                    for node_output in update.values():
                        messages = (
                            node_output.get("messages", [])
                            if isinstance(node_output, dict) else []
                        )
                        for msg in messages:
                            if isinstance(msg, AIMessage):
                                tool_calls = getattr(msg, "tool_calls", []) or []
                                for tc in tool_calls:
                                    call_id = tc.get("id", "") or ""
                                    if call_id in seen_call_ids:
                                        continue
                                    seen_call_ids.add(call_id)
                                    call_args_sigs[call_id] = _tool_args_sig(
                                        tc.get("args", {})
                                    )
                                    yield AgentEventToolCallStarted(
                                        name=tc.get("name", "") or "",
                                        args=tc.get("args", {}) or {},
                                        call_id=call_id,
                                    )
                                # Track the latest textual AI message so we emit
                                # the right "final" text at the end. v0.20.1 —
                                # relaxed the gate from ``text and not
                                # tool_calls`` to just ``text``. The strict
                                # gate dropped legitimate intermediate prose
                                # ("Let me check the docs...") when the model
                                # ALSO returned an empty terminal AIMessage
                                # after the tool call — gemma does this
                                # routinely after ``knowledge.search``,
                                # leaving the cockpit with an empty bubble
                                # the operator could only fix by sending a
                                # second turn. The terminal AIMessage's text
                                # still wins when present (it lands later in
                                # the stream and overwrites); the change
                                # only matters when the terminal is empty.
                                text = _ai_text(msg)
                                if text:
                                    last_ai_text = text
                                # v0.12 — accumulate per-turn token
                                # counts so the chat route can record
                                # cost via chat_caps.record_chat_turn.
                                # langchain_openai surfaces usage_metadata
                                # on the final assistant message; some
                                # providers also emit on tool-call AI
                                # messages, so accumulate across all of
                                # them.
                                um = getattr(msg, "usage_metadata", None)
                                if um:
                                    usage_total["input_tokens"] += int(
                                        um.get("input_tokens") or 0
                                    )
                                    usage_total["output_tokens"] += int(
                                        um.get("output_tokens") or 0
                                    )
                                rm = getattr(msg, "response_metadata", None) or {}
                                model_name = rm.get("model_name") or rm.get("model")
                                if model_name:
                                    usage_total["model"] = model_name
                            elif isinstance(msg, ToolMessage):
                                full = (
                                    str(msg.content) if msg.content is not None else ""
                                )
                                is_error = (
                                    getattr(msg, "status", None) == "error"
                                )
                                yield AgentEventToolCallCompleted(
                                    name=msg.name or "",
                                    call_id=msg.tool_call_id or "",
                                    result=_truncate(full),
                                    result_full=full,
                                    is_error=is_error,
                                )
                                # v1.5 — 3-strike bail. Track consecutive
                                # failure-shaped results across the loop.
                                # A run of three (real exceptions OR
                                # structured POLICY_VIOLATION /
                                # DESTRUCTIVE_OPERATION_BLOCKED /
                                # CLIENT_ERROR results) terminates the
                                # stream with a clear AgentEventError so
                                # the cockpit can render a "couldn't
                                # recover" panel instead of letting the
                                # LLM thrash forever. Counter resets on
                                # any success.
                                if _is_failure_tool_result(full, is_error):
                                    consecutive_failures += 1
                                    if consecutive_failures >= MAX_CONSECUTIVE_TOOL_FAILURES:
                                        yield AgentEventError(
                                            message=(
                                                f"agent_loop_bailout: "
                                                f"{MAX_CONSECUTIVE_TOOL_FAILURES} "
                                                f"consecutive tool failures "
                                                f"(last tool: {msg.name or 'unknown'!r}). "
                                                f"The agent could not recover — "
                                                f"send a fresh prompt or rephrase."
                                            )
                                        )
                                        return
                                else:
                                    consecutive_failures = 0
                                # v1.6.2 — stuck-loop bail. Identical
                                # call + identical result three times
                                # running means the agent is replaying,
                                # not progressing — the failure counter
                                # above never fires when each replay
                                # "succeeds" (customer.list → [] was
                                # the incident shape).
                                if stuck_tracker.record(
                                    msg.name or "",
                                    call_args_sigs.pop(
                                        msg.tool_call_id or "", ""
                                    ),
                                    full,
                                ):
                                    yield AgentEventError(
                                        message=(
                                            f"agent_loop_bailout: "
                                            f"{MAX_CONSECUTIVE_IDENTICAL_TOOL_CALLS} "
                                            f"identical calls to "
                                            f"{msg.name or 'unknown'!r} "
                                            f"returned the identical result. "
                                            f"The agent is stuck — rephrase, "
                                            f"or give it a different "
                                            f"identifier to work with."
                                        )
                                    )
                                    return
                                # v0.12 PR4 — output ownership trip-wire.
                                # Skips error-status results (they cannot
                                # carry customer-bound rows by definition)
                                # and runs only when an actor is bound
                                # (the chat surface) so non-chat callers
                                # (CLI / scenario / CSR) keep their
                                # full-surface behaviour.
                                if actor and not is_error and msg.name:
                                    try:
                                        assert_owned_output(
                                            tool_name=msg.name,
                                            result_json=full,
                                            actor=actor,
                                        )
                                    except AgentOwnershipViolation as v:
                                        # Best-effort audit trail
                                        # (record_violation has its own
                                        # try/except — never raises).
                                        # Then surface to the route
                                        # handler which renders the
                                        # generic user-facing message.
                                        await record_violation(
                                            crm_client=get_clients().crm,
                                            actor=actor,
                                            tool_name=v.tool_name,
                                            path=v.path,
                                            found=v.found,
                                            transcript_so_far=prompt,
                                        )
                                        yield AgentEventError(
                                            message=(
                                                f"AgentOwnershipViolation: "
                                                f"{v.tool_name}"
                                            )
                                        )
                                        return
            except Exception as exc:  # noqa: BLE001
                yield AgentEventError(message=f"{type(exc).__name__}: {exc}")
                return

            # v0.12 — emit token totals BEFORE FinalMessage so the
            # chat route can call chat_caps.record_chat_turn before
            # closing the SSE response. Earlier we tried ordering
            # this AFTER FinalMessage, but the chat-route's SSE
            # consumer (browsers, soak runner) disconnects on the
            # "status: done" frame the FinalMessage triggers; the
            # next yield then raises GeneratorExit and the housekeeping
            # never lands. Putting TurnUsage first keeps cost
            # accounting honest.
            yield AgentEventTurnUsage(
                prompt_tok=usage_total["input_tokens"],
                completion_tok=usage_total["output_tokens"],
                model=usage_total["model"] or "",
            )
            yield AgentEventFinalMessage(text=last_ai_text)
        finally:
            # v0.20.1 — guard ContextVar.reset against cross-context
            # disposal. Starlette closes the SSE response by calling
            # ``aclose`` on the async generator from a *different*
            # asyncio Task than the one the body iterated in; that
            # different Task carries a different copy_context()
            # snapshot, and Python's ``ContextVar.reset(token)``
            # raises ``ValueError: Token was created in a different
            # Context``. The unhandled ValueError propagated up
            # through Starlette and closed the connection before the
            # browser had committed the final ``message`` / ``status:
            # done`` frames — the operator saw the prior turn's
            # bubble appear only on the *next* request when the page
            # rehydrated from persistence. The tokens are best-effort
            # cleanup; a ValueError here means the Context is going
            # away anyway, so swallow it.
            if identity_reset_token is not None:
                try:
                    reset_service_identity_token(identity_reset_token)
                except ValueError:
                    pass
            if auth_reset_token is not None:
                try:
                    auth_context.reset_actor(auth_reset_token)
                except ValueError:
                    pass


def _messages_from_transcript(transcript: str) -> list[BaseMessage]:
    """Parse a Conversation.transcript_text() string into typed messages.

    The cockpit's transcript format (see
    ``bss_cockpit.conversation.Conversation.transcript_text``):

        user:
        hello

        assistant:
        hi there

        tool[customer.get]:
        {"id": "C-1"}

    Mapping:
    - ``user:`` blocks → :class:`HumanMessage`
    - ``assistant:`` blocks → :class:`AIMessage`
    - ``tool[NAME]:`` blocks → :class:`SystemMessage` (a brief
      "(prior tool result for NAME)" header + the body). We don't
      reconstruct ToolMessage with a ``tool_call_id`` because that
      field has to pair with a prior AIMessage's tool_calls; faking
      one breaks LangGraph's assertions. SystemMessage gives the
      model the same information without the structural lie.

    Empty / malformed input returns ``[]`` — callers fall through to
    the single-HumanMessage path. Robustness over fidelity: a
    transcript that fails to parse should never break a turn.

    Truncation: if the transcript exceeds ``_TRANSCRIPT_MAX_CHARS``,
    keep the most recent suffix and prepend an elided-marker turn.
    Doctrine "the trap" — long-running cockpit sessions feeding 50k
    chars of transcript every turn is a token-cost trap. Cap here.
    """
    if not transcript or not transcript.strip():
        return []

    if len(transcript) > _TRANSCRIPT_MAX_CHARS:
        transcript = (
            f"[…earlier turns elided to keep prompt within "
            f"{_TRANSCRIPT_MAX_CHARS} chars; ask the operator to "
            f"/reset if continuity matters…]\n\n"
            + transcript[-_TRANSCRIPT_MAX_CHARS:]
        )

    out: list[BaseMessage] = []
    # Each turn is ``role:\ncontent`` separated by blank lines. Split
    # on the canonical ``\n\n`` joiner first; the parser is forgiving
    # about extra whitespace.
    for block in transcript.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        head, _, body = block.partition("\n")
        head = head.strip().rstrip(":")
        body = body.strip()
        if not head:
            continue
        if head == "user":
            out.append(HumanMessage(content=body))
        elif head == "assistant":
            out.append(AIMessage(content=body))
        elif head.startswith("tool"):
            # tool[customer.get]: → SystemMessage with a prior-result note.
            tool_name = ""
            if head.startswith("tool[") and head.endswith("]"):
                tool_name = head[len("tool["):-1]
            label = (
                f"prior tool result for {tool_name}"
                if tool_name else "prior tool result"
            )
            out.append(SystemMessage(content=f"({label}):\n{body}"))
        # Unknown roles are skipped silently — better than crashing
        # the turn on a future role we haven't added a mapping for.
    return out


# Cap on the prior transcript fed back to the LLM each turn. Chosen so
# a 50-turn cockpit session is still served (each turn is ~200-500
# chars typically), but a runaway debug dump doesn't blow up token
# cost. Operator can /reset to clear messages on the same session id.
_TRANSCRIPT_MAX_CHARS = 32_000


def _ai_text(msg: AIMessage) -> str:
    """Extract the text of an AIMessage (handles string + list-of-parts content)."""
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        )
    return ""
