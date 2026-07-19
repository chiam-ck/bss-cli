"""`bss trace ...` — Jaeger trace lookup + ASCII swimlane (v0.2)."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from bss_clients import AuditClient, TokenAuthProvider
from bss_cockpit.renderers.trace import render_swimlane
from bss_middleware import api_token
from bss_orchestrator.config import settings
from rich import print as rprint

from .._runtime import run_async
from ..jaeger import JaegerClient, JaegerError

app = typer.Typer(help="Query Jaeger traces + audit events.", no_args_is_help=True)


@app.command("get")
def get_trace_cmd(
    trace_id: Annotated[str, typer.Argument(help="32-char hex trace ID.")],
    width: Annotated[int | None, typer.Option("--width", help="Override terminal width.")] = None,
    show_sql: Annotated[bool, typer.Option("--show-sql", help="Include SQL spans.")] = False,
    only_service: Annotated[str | None, typer.Option("--service", help="Filter to one service.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit raw Jaeger JSON.")] = False,
) -> None:
    """Render the swimlane for a trace ID."""
    async def _run() -> None:
        async with JaegerClient() as jc:
            try:
                trace = await jc.get_trace(trace_id)
            except JaegerError as exc:
                rprint(f"[red]{exc}[/]")
                raise typer.Exit(code=2)
        _emit(trace, as_json=as_json, width=width, show_sql=show_sql, only_service=only_service)

    run_async(_run())


@app.command("for-order")
def for_order_cmd(
    order_id: Annotated[str, typer.Argument(help="Commercial order ID, e.g. ORD-014.")],
    width: Annotated[int | None, typer.Option("--width")] = None,
    show_sql: Annotated[bool, typer.Option("--show-sql")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit raw Jaeger JSON.")] = False,
) -> None:
    """Resolve trace_id from audit events for an order, then render."""
    async def _run() -> None:
        async with AuditClient(
            base_url=settings.com_url,
            auth_provider=TokenAuthProvider(api_token()),
        ) as ac:
            events = await ac.list_events(
                aggregate_type="ProductOrder",
                aggregate_id=order_id,
                limit=20,
            )
        trace_id = _latest_trace_id(events)
        if not trace_id:
            rprint(
                f"[yellow]no trace_id recorded for order {order_id} "
                f"(was it created before v0.2?)[/]"
            )
            raise typer.Exit(code=2)
        async with JaegerClient() as jc:
            try:
                trace = await jc.get_trace(trace_id)
            except JaegerError as exc:
                rprint(f"[red]{exc}[/]")
                raise typer.Exit(code=2)
        _emit(trace, as_json=as_json, width=width, show_sql=show_sql)

    run_async(_run())


@app.command("for-subscription")
def for_subscription_cmd(
    subscription_id: Annotated[str, typer.Argument(help="Subscription ID, e.g. SUB-007.")],
    width: Annotated[int | None, typer.Option("--width")] = None,
    show_sql: Annotated[bool, typer.Option("--show-sql")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit raw Jaeger JSON.")] = False,
) -> None:
    """Resolve trace_id from audit events for a subscription, then render."""
    async def _run() -> None:
        async with AuditClient(
            base_url=settings.subscription_url,
            auth_provider=TokenAuthProvider(api_token()),
        ) as ac:
            events = await ac.list_events(
                aggregate_type="subscription",
                aggregate_id=subscription_id,
                limit=20,
            )
        trace_id = _latest_trace_id(events)
        if not trace_id:
            rprint(
                f"[yellow]no trace_id recorded for subscription {subscription_id} "
                f"(was it created before v0.2?)[/]"
            )
            raise typer.Exit(code=2)
        async with JaegerClient() as jc:
            try:
                trace = await jc.get_trace(trace_id)
            except JaegerError as exc:
                rprint(f"[red]{exc}[/]")
                raise typer.Exit(code=2)
        _emit(trace, as_json=as_json, width=width, show_sql=show_sql)

    run_async(_run())


@app.command("for-ask")
def for_ask_cmd(
    width: Annotated[int | None, typer.Option("--width")] = None,
    show_sql: Annotated[bool, typer.Option("--show-sql")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit raw Jaeger JSON.")] = False,
) -> None:
    """Render the most-recent ``bss ask`` trace."""
    async def _run() -> None:
        async with JaegerClient() as jc:
            tid = await jc.latest_ask_trace_id()
            if not tid:
                rprint("[yellow]no recent bss.ask trace found in Jaeger[/]")
                raise typer.Exit(code=2)
            try:
                trace = await jc.get_trace(tid)
            except JaegerError as exc:
                rprint(f"[red]{exc}[/]")
                raise typer.Exit(code=2)
        _emit(trace, as_json=as_json, width=width, show_sql=show_sql)

    run_async(_run())


@app.command("services")
def services_cmd() -> None:
    """List services currently exporting traces to Jaeger."""
    async def _run() -> None:
        async with JaegerClient() as jc:
            try:
                names = await jc.list_services()
            except JaegerError as exc:
                rprint(f"[red]{exc}[/]")
                raise typer.Exit(code=2)
        for name in sorted(names):
            print(name)

    run_async(_run())


def _emit(
    trace: dict[str, Any],
    *,
    as_json: bool,
    width: int | None = None,
    show_sql: bool = False,
    only_service: str | None = None,
) -> None:
    """Print either the raw Jaeger JSON or the rendered swimlane."""
    if as_json:
        print(json.dumps(trace, indent=2))
        return
    print(render_swimlane(trace, width=width, show_sql=show_sql, only_service=only_service))


def _latest_trace_id(events: list[dict]) -> str | None:
    """Return the trace_id of the most recent event with one set.

    Audit events come back from /audit-api/v1/events sorted by
    occurred_at ASC, so we walk from the end. We want the latest
    trace because the "completion" trace (with the manual span
    fan-out — som.decompose, com.order.complete_to_subscription)
    is more interesting on the swimlane than the initial create.
    """
    for ev in reversed(events):
        tid = ev.get("traceId") or ev.get("trace_id")
        if tid:
            return tid
    return None
