"""Ops / observability tools — clock, trace, events, agents.

Clock/trace/events are thin scaffolding in v0.1. A dedicated scenario clock
service and the event-bus query surface arrive in Phase 11; until then the
tools return structured ``NOT_IMPLEMENTED`` payloads so the LLM can explain
the gap to the user rather than crashing mid-turn. ``clock.now`` and the
``agents.*`` tools are live.
"""

from __future__ import annotations

from typing import Any

from bss_clients import AuditClient, TokenAuthProvider

# Alias must not collide with the clock_now tool below — the def used
# to shadow this import, so the tool body called ITSELF and every
# clock.now invocation died with "'coroutine' object has no attribute
# 'replace'" (found via F811, 2026-06-12).
from bss_clock import now as _bss_clock_now
from bss_middleware import api_token
from bss_telemetry import JaegerClient, JaegerError

from ..clients import get_clients
from ..config import settings
from ..types import (
    AgentState,
    AggregateId,
    AggregateType,
    IsoDatetime,
    OrderId,
    SubscriptionId,
    TraceId,
)
from ._registry import register

_CLOCK_NOT_IMPLEMENTED = {
    "error": "NOT_IMPLEMENTED",
    "message": (
        "Virtual clock control ships in Phase 11 (scenario runner). In v0.1 "
        "the system clock is authoritative — use clock.now to read it."
    ),
}

_EVENTS_NOT_IMPLEMENTED = {
    "error": "NOT_IMPLEMENTED",
    "message": (
        "Event-bus query tools ship in Phase 11. Events are already persisted "
        "to audit.domain_event in every service schema; query them via SQL for now."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Clock
# ─────────────────────────────────────────────────────────────────────────────


@register("clock.now")
async def clock_now() -> dict[str, Any]:
    """Return the current wall-clock time as ISO-8601 UTC. Use this whenever
    a tool needs a ``since`` / ``event_time`` value — do NOT fabricate timestamps.

    Args:
        (none)

    Returns:
        ``{now: IsoDatetime, source: "system"}``. In Phase 11 ``source`` will
        become ``"scenario"`` when a virtual clock is active.

    Raises:
        (none)
    """
    now = _bss_clock_now().replace(microsecond=0).isoformat()
    return {"now": now, "source": "system"}


@register("clock.advance")
async def clock_advance(duration: str) -> dict[str, Any]:
    """Advance the scenario clock by a duration. NOT IMPLEMENTED in v0.1.

    Args:
        duration: Duration string, e.g. ``"1h"``, ``"30d"``.

    Returns:
        v0.1: structured ``NOT_IMPLEMENTED`` payload.

    Raises:
        (none in v0.1)
    """
    return {**_CLOCK_NOT_IMPLEMENTED, "duration": duration}


@register("clock.freeze")
async def clock_freeze(at: IsoDatetime | None = None) -> dict[str, Any]:
    """Freeze the scenario clock. NOT IMPLEMENTED in v0.1.

    Args:
        at: Optional ISO-8601 instant to freeze at; defaults to ``now``.

    Returns:
        v0.1: structured ``NOT_IMPLEMENTED`` payload.

    Raises:
        (none in v0.1)
    """
    return {**_CLOCK_NOT_IMPLEMENTED, "requestedAt": at}


@register("clock.unfreeze")
async def clock_unfreeze() -> dict[str, Any]:
    """Resume the scenario clock. NOT IMPLEMENTED in v0.1.

    Args:
        (none)

    Returns:
        v0.1: structured ``NOT_IMPLEMENTED`` payload.

    Raises:
        (none in v0.1)
    """
    return dict(_CLOCK_NOT_IMPLEMENTED)


# ─────────────────────────────────────────────────────────────────────────────
# Trace / events (Phase 11 stubs)
# ─────────────────────────────────────────────────────────────────────────────


def _summarize_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Jaeger v1 trace dict to LLM/scenario-friendly summary fields."""
    spans = trace.get("spans", [])
    processes = trace.get("processes", {})
    services = sorted({
        processes.get(s.get("processID", ""), {}).get("serviceName", "?")
        for s in spans
    })
    error_count = sum(
        1
        for s in spans
        for tag in s.get("tags", [])
        if tag.get("key") == "error" and tag.get("value") is True
    )
    if spans:
        start = min(int(s.get("startTime", 0)) for s in spans)
        end = max(int(s.get("startTime", 0)) + int(s.get("duration", 0)) for s in spans)
        total_us = end - start
    else:
        total_us = 0
    return {
        "traceId": trace.get("traceID", ""),
        "spanCount": len(spans),
        "serviceCount": len(services),
        "services": services,
        "errorSpanCount": error_count,
        "totalMs": round(total_us / 1000.0, 2),
    }


@register("trace.get")
async def trace_get(trace_id: TraceId) -> dict[str, Any]:
    """Fetch a Jaeger trace by ID and return a summary suitable for LLM /
    scenario assertions. The full Jaeger payload is large; this returns
    only the structural fields most useful for "did the trace look right?"
    questions. Use the ``bss trace get <id>`` CLI for the human-readable
    swimlane.

    Args:
        trace_id: 32-char hex trace ID.

    Returns:
        ``{traceId, spanCount, serviceCount, services, errorSpanCount, totalMs}``.

    Raises:
        ToolException: trace not found in Jaeger or the backend is unreachable.
    """
    async with JaegerClient() as jc:
        try:
            trace = await jc.get_trace(trace_id)
        except JaegerError as exc:
            return {"error": "JAEGER_ERROR", "message": str(exc), "traceId": trace_id}
    return _summarize_trace(trace)


@register("trace.for_order")
async def trace_for_order(order_id: OrderId) -> dict[str, Any]:
    """Resolve the most-recent trace for an order via audit.domain_event,
    then fetch + summarize from Jaeger. Use this to investigate "what
    happened during this order's processing".

    Args:
        order_id: Commercial Order ID with the ORD- prefix (opaque suffix).

    Returns:
        Summary dict per ``trace.get``, plus ``orderId``. ``error`` field
        when no trace_id was recorded (pre-v0.2 order) or Jaeger missing.

    Raises:
        ToolException: audit-events endpoint unreachable.
    """
    async with AuditClient(
        base_url=settings.com_url, auth_provider=TokenAuthProvider(api_token())
    ) as ac:
        events = await ac.list_events(
            aggregate_type="ProductOrder", aggregate_id=order_id, limit=20
        )
    trace_id = _latest_trace_id(events)
    if not trace_id:
        return {
            "error": "NO_TRACE_RECORDED",
            "message": f"no trace_id on any audit event for {order_id}",
            "orderId": order_id,
        }
    summary = await trace_get(trace_id)
    summary["orderId"] = order_id
    return summary


@register("trace.for_subscription")
async def trace_for_subscription(subscription_id: SubscriptionId) -> dict[str, Any]:
    """Resolve the most-recent trace for a subscription via audit.domain_event,
    then fetch + summarize from Jaeger.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).

    Returns:
        Summary dict per ``trace.get``, plus ``subscriptionId``.

    Raises:
        ToolException: audit-events endpoint unreachable.
    """
    async with AuditClient(
        base_url=settings.subscription_url,
        auth_provider=TokenAuthProvider(api_token()),
    ) as ac:
        events = await ac.list_events(
            aggregate_type="subscription", aggregate_id=subscription_id, limit=20
        )
    trace_id = _latest_trace_id(events)
    if not trace_id:
        return {
            "error": "NO_TRACE_RECORDED",
            "message": f"no trace_id on any audit event for {subscription_id}",
            "subscriptionId": subscription_id,
        }
    summary = await trace_get(trace_id)
    summary["subscriptionId"] = subscription_id
    return summary


def _latest_trace_id(events: list[dict[str, Any]]) -> str | None:
    for ev in reversed(events):
        tid = ev.get("traceId") or ev.get("trace_id")
        if tid:
            return tid
    return None


@register("events.list")
async def events_list(
    aggregate_type: AggregateType | None = None,
    aggregate_id: AggregateId | None = None,
    since: IsoDatetime | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List recent domain events. NOT IMPLEMENTED in v0.1.

    Args:
        aggregate_type: Optional filter by aggregate kind.
        aggregate_id: Optional filter by aggregate ID.
        since: Optional ISO-8601 lower bound.
        limit: Max rows (default 50).

    Returns:
        v0.1: structured ``NOT_IMPLEMENTED`` payload.

    Raises:
        (none in v0.1)
    """
    return {
        **_EVENTS_NOT_IMPLEMENTED,
        "aggregateType": aggregate_type,
        "aggregateId": aggregate_id,
        "since": since,
        "limit": limit,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Agents (CRM)
# ─────────────────────────────────────────────────────────────────────────────


@register("agents.list")
async def agents_list(state: AgentState | None = None) -> list[dict[str, Any]]:
    """List CSR/support agents. Used before case/ticket assignment to pick a
    valid assignee.

    Args:
        state: Optional filter — typically ``"active"``.

    Returns:
        List of agent dicts ``{id, name, email, state, roles}``.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().crm.list_agents(state=state)


