"""`bss ticket ...` — trouble-ticket lifecycle commands."""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_cockpit.renderers import render_ticket
from bss_orchestrator.clients import get_clients
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Manage trouble tickets (TMF621).", no_args_is_help=True)


@app.command("open")
def open_(
    type_: Annotated[str, typer.Option("--type")],
    subject: Annotated[str, typer.Option("--subject")],
    case: Annotated[str | None, typer.Option("--case")] = None,
    customer: Annotated[str | None, typer.Option("--customer")] = None,
) -> None:
    """Open a trouble ticket, optionally linked to a case/customer."""

    async def _do() -> None:
        c = get_clients()
        t = await c.crm.open_ticket(
            ticket_type=type_,
            subject=subject,
            case_id=case,
            customer_id=customer,
        )
        rprint(f"[green]Opened[/] {t['id']}  {t['ticketType']}  [{t['state']}]")

    _run_safely(_do())


@app.command("list")
def list_(
    case: Annotated[str | None, typer.Option("--case")] = None,
    customer: Annotated[str | None, typer.Option("--customer")] = None,
    state: Annotated[str | None, typer.Option("--state")] = None,
    agent: Annotated[str | None, typer.Option("--agent")] = None,
) -> None:
    """List trouble tickets, optionally filtered."""

    async def _do() -> None:
        c = get_clients()
        rows = await c.crm.list_tickets(
            case_id=case,
            customer_id=customer,
            state=state,
            agent_id=agent,
        )
        for r in rows:
            rprint(
                f"{r['id']:<8}  {r.get('ticketType', ''):<18} "
                f"{r.get('state', ''):<12} {r.get('priority', ''):<6} "
                f"{r.get('assignedAgent') or '—'}"
            )

    _run_safely(_do())


@app.command("show")
def show(ticket_id: Annotated[str, typer.Argument()]) -> None:
    """Show a trouble ticket."""

    async def _do() -> None:
        c = get_clients()
        t = await c.crm.get_ticket(ticket_id)
        print(render_ticket(t))

    _run_safely(_do())


@app.command("assign")
def assign(
    ticket_id: Annotated[str, typer.Argument()],
    agent: Annotated[str, typer.Option("--agent")],
) -> None:
    """Assign a ticket to an agent."""

    async def _do() -> None:
        c = get_clients()
        out = await c.crm.assign_ticket(ticket_id, agent_id=agent)
        rprint(f"[green]Assigned[/] {out['id']} → {out.get('assignedAgent')}")

    _run_safely(_do())


@app.command("ack")
def ack(ticket_id: Annotated[str, typer.Argument()]) -> None:
    """Transition ticket → acknowledged."""

    async def _do() -> None:
        c = get_clients()
        out = await c.crm.transition_ticket(ticket_id, to_state="acknowledged")
        rprint(f"[green]{out['id']}[/] → {out['state']}")

    _run_safely(_do())


@app.command("start")
def start(ticket_id: Annotated[str, typer.Argument()]) -> None:
    """Transition ticket → in_progress."""

    async def _do() -> None:
        c = get_clients()
        out = await c.crm.transition_ticket(ticket_id, to_state="in_progress")
        rprint(f"[green]{out['id']}[/] → {out['state']}")

    _run_safely(_do())


@app.command("resolve")
def resolve(
    ticket_id: Annotated[str, typer.Argument()],
    notes: Annotated[str, typer.Option("--notes")],
) -> None:
    """Resolve a ticket with resolution notes."""

    async def _do() -> None:
        c = get_clients()
        out = await c.crm.resolve_ticket(ticket_id, resolution_notes=notes)
        rprint(f"[green]{out['id']}[/] → {out['state']}")

    _run_safely(_do())


@app.command("close")
def close(ticket_id: Annotated[str, typer.Argument()]) -> None:
    """Transition ticket → closed."""

    async def _do() -> None:
        c = get_clients()
        out = await c.crm.close_ticket(ticket_id)
        rprint(f"[green]{out['id']}[/] → {out['state']}")

    _run_safely(_do())


@app.command("cancel")
def cancel(
    ticket_id: Annotated[str, typer.Argument()],
    allow_destructive: Annotated[bool, typer.Option("--allow-destructive")] = False,
) -> None:
    """Cancel a ticket (destructive)."""
    if not allow_destructive:
        rprint("[yellow]cancel is gated behind --allow-destructive.[/]")
        raise typer.Exit(code=2)

    async def _do() -> None:
        c = get_clients()
        out = await c.crm.cancel_ticket(ticket_id)
        rprint(f"[green]{out['id']}[/] → {out['state']}")

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
