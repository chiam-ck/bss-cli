"""`bss som ...` — SOM inventory inspection commands."""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_orchestrator.clients import get_clients
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Service order + service inventory (SOM).", no_args_is_help=True)

service_app = typer.Typer(help="Service inventory (TMF638).")
app.add_typer(service_app, name="service")


@service_app.command("list")
def service_list(
    subscription: Annotated[str, typer.Option("--subscription")],
) -> None:
    """List services belonging to a subscription (CFS + RFS tree flat)."""

    async def _do() -> None:
        c = get_clients()
        for s in await c.som.list_services_for_subscription(subscription):
            rprint(
                f"{s['id']:<9} {s.get('serviceType', ''):<4} "
                f"{s.get('name', ''):<22} {s.get('state', '')}"
            )

    _run_safely(_do())


@app.command("service-show")
def service_show(service_id: Annotated[str, typer.Argument()]) -> None:
    """Show a single service (JSON)."""

    async def _do() -> None:
        c = get_clients()
        rprint(await c.som.get_service(service_id))

    _run_safely(_do())


@app.command("so-show")
def so_show(service_order_id: Annotated[str, typer.Argument()]) -> None:
    """Show a service order (JSON)."""

    async def _do() -> None:
        c = get_clients()
        rprint(await c.som.get_service_order(service_order_id))

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
