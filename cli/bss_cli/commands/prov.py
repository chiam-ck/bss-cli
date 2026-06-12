"""`bss prov ...` — provisioning-sim commands (tasks + fault injection)."""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_cockpit.renderers import render_prov_tasks
from bss_orchestrator.clients import get_clients
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Provisioning-sim: tasks + fault injection.", no_args_is_help=True)


@app.command("tasks")
def tasks(
    service: Annotated[str | None, typer.Option("--service")] = None,
    state: Annotated[str | None, typer.Option("--state")] = None,
) -> None:
    """List provisioning tasks."""

    async def _do() -> None:
        c = get_clients()
        ts = await c.provisioning.list_tasks(service_id=service, state=state)
        print(render_prov_tasks(ts))

    _run_safely(_do())


@app.command("show")
def show(task_id: Annotated[str, typer.Argument()]) -> None:
    """Show a single provisioning task."""

    async def _do() -> None:
        c = get_clients()
        rprint(await c.provisioning.get_task(task_id))

    _run_safely(_do())


@app.command("resolve")
def resolve(
    task_id: Annotated[str, typer.Argument()],
    note: Annotated[str, typer.Option("--note")],
) -> None:
    """Manually resolve a stuck provisioning task."""

    async def _do() -> None:
        c = get_clients()
        out = await c.provisioning.resolve_task(task_id, note=note)
        rprint(f"[green]{out['id']}[/] → {out['state']}")

    _run_safely(_do())


@app.command("retry")
def retry(task_id: Annotated[str, typer.Argument()]) -> None:
    """Retry a failed provisioning task."""

    async def _do() -> None:
        c = get_clients()
        out = await c.provisioning.retry_task(task_id)
        rprint(f"[green]{out['id']}[/] → {out['state']}  attempts={out.get('attempts')}")

    _run_safely(_do())


@app.command("fault")
def fault(
    task_type: Annotated[str, typer.Argument(help="e.g. HLR_PROVISION")],
    fault_type: Annotated[str, typer.Argument(help="fail_first_attempt|fail_always|stuck|slow")],
    enable: Annotated[bool, typer.Option("--enable/--disable")] = True,
    probability: Annotated[float | None, typer.Option("--probability")] = None,
    allow_destructive: Annotated[bool, typer.Option("--allow-destructive")] = False,
) -> None:
    """Toggle/adjust fault-injection for a task type (admin-ish → destructive)."""
    if not allow_destructive:
        rprint("[yellow]fault is gated behind --allow-destructive.[/]")
        raise typer.Exit(code=2)

    async def _do() -> None:
        c = get_clients()
        injectors = await c.provisioning.list_fault_injection()
        target = next(
            (i for i in injectors if i["taskType"] == task_type and i["faultType"] == fault_type),
            None,
        )
        if not target:
            rprint(f"[red]No fault-injection for {task_type}/{fault_type}[/]")
            raise typer.Exit(code=2)
        out = await c.provisioning.update_fault_injection(
            target["id"],
            enabled=enable,
            probability=probability,
        )
        rprint(f"[green]{out['id']}[/] enabled={out['enabled']} p={out.get('probability')}")

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
