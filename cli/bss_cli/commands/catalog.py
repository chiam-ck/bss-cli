"""`bss catalog ...` — product catalog commands."""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_cockpit.renderers import render_catalog
from bss_orchestrator.clients import get_clients
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Browse the product catalog (TMF620).", no_args_is_help=True)


@app.command("list")
def list_() -> None:
    """Render the three-column plan comparison."""

    async def _do() -> None:
        c = get_clients()
        offerings = await c.catalog.list_offerings()
        print(render_catalog(offerings))

    _run_safely(_do())


@app.command("vas")
def vas() -> None:
    """List VAS offerings."""

    async def _do() -> None:
        c = get_clients()
        for v in await c.catalog.list_vas():
            price = v.get("priceAmount", v.get("price", "?"))
            ccy = v.get("currency", "SGD")
            rprint(f"{v['id']:<20} {v.get('name', ''):<28} {ccy} {price}")

    _run_safely(_do())


@app.command("show")
def show(offering_id: Annotated[str, typer.Argument()]) -> None:
    """Show a single offering (JSON)."""

    async def _do() -> None:
        c = get_clients()
        rprint(await c.catalog.get_offering(offering_id))

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
