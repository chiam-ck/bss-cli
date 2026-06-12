"""`bss case ...` — case lifecycle commands."""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_cockpit.renderers import render_case
from bss_orchestrator.clients import get_clients
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Manage cases (CRM).", no_args_is_help=True)


@app.command("open")
def open_(
    customer: Annotated[str, typer.Option("--customer")],
    subject: Annotated[str, typer.Option("--subject")],
    category: Annotated[str, typer.Option("--category")] = "technical",
    priority: Annotated[str, typer.Option("--priority")] = "medium",
) -> None:
    """Open a new case against a customer."""

    async def _do() -> None:
        c = get_clients()
        case = await c.crm.open_case(
            customer_id=customer,
            subject=subject,
            category=category,
            priority=priority,
        )
        rprint(f"[green]Opened[/] {case['id']}  {case['subject']!r}  [{case['state']}]")

    _run_safely(_do())


@app.command("list")
def list_(
    customer: Annotated[str | None, typer.Option("--customer")] = None,
    state: Annotated[str | None, typer.Option("--state")] = None,
) -> None:
    """List cases, optionally filtered."""

    async def _do() -> None:
        c = get_clients()
        rows = await c.crm.list_cases(customer_id=customer, state=state)
        for r in rows:
            rprint(
                f"{r['id']:<9}  {r.get('subject', '')[:30]:<30} "
                f"{r.get('priority', ''):<7} {r.get('state', '')}"
            )

    _run_safely(_do())


@app.command("show")
def show(case_id: Annotated[str, typer.Argument()]) -> None:
    """Render a case with its tickets and notes."""

    async def _do() -> None:
        c = get_clients()
        case = await c.crm.get_case(case_id)
        tickets = await c.crm.list_tickets(case_id=case_id)
        notes = case.get("notes") or []
        print(render_case(case, tickets=tickets, notes=notes))

    _run_safely(_do())


@app.command("close")
def close(
    case_id: Annotated[str, typer.Argument()],
    resolution: Annotated[str, typer.Option("--resolution")] = "resolved",
    allow_destructive: Annotated[bool, typer.Option("--allow-destructive")] = False,
) -> None:
    """Close a case (policy: no open tickets)."""
    if not allow_destructive:
        rprint("[yellow]close is gated behind --allow-destructive.[/]")
        raise typer.Exit(code=2)

    async def _do() -> None:
        c = get_clients()
        out = await c.crm.close_case(case_id, resolution_code=resolution)
        rprint(f"[green]Closed[/] {out['id']}  [{out['state']}]")

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
