"""`bss inventory ...` — MSISDN + eSIM pool browsing + replenishment.

Inventory is hosted inside the CRM service (port 8002) under
``/inventory-api/v1/``. List/show commands are read-only — reservation
and assignment happen as side effects of order activation in SOM. The
v0.17 ``msisdn add-range`` command is operator-only (no customer
self-serve path) and writes directly via ``InventoryClient``.
"""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_orchestrator.clients import get_clients
from rich import print as rprint
from rich.table import Table

from .._runtime import run_async

app = typer.Typer(
    help="Browse MSISDN + eSIM inventory pools (read-only).",
    no_args_is_help=True,
)

msisdn_app = typer.Typer(help="MSISDN pool.", no_args_is_help=True)
esim_app = typer.Typer(help="eSIM profile pool.", no_args_is_help=True)
app.add_typer(msisdn_app, name="msisdn")
app.add_typer(esim_app, name="esim")


# ── MSISDN ──────────────────────────────────────────────────────────

@msisdn_app.command("list")
def msisdn_list(
    state: Annotated[
        str | None,
        typer.Option("--state", help="available | reserved | assigned | released"),
    ] = None,
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="MSISDN prefix filter.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
) -> None:
    """List MSISDNs in the pool."""

    async def _do() -> None:
        rows = await get_clients().inventory.list_msisdns(
            state=state, prefix=prefix, limit=limit
        )
        if not rows:
            rprint("[yellow]no MSISDNs match[/]")
            return
        table = Table(title=f"MSISDN pool ({len(rows)} shown)")
        table.add_column("msisdn")
        table.add_column("state")
        table.add_column("assigned to")
        for r in rows:
            table.add_row(
                r.get("msisdn", "?"),
                r.get("status", r.get("state", "?")),
                r.get("assigned_to_subscription_id") or "—",
            )
        rprint(table)

    _run_safely(_do())


@msisdn_app.command("show")
def msisdn_show(msisdn: Annotated[str, typer.Argument(help="MSISDN, e.g. +6581234567")]) -> None:
    """Show one MSISDN (JSON)."""

    async def _do() -> None:
        rprint(await get_clients().inventory.get_msisdn(msisdn))

    _run_safely(_do())


@msisdn_app.command("add-range")
def msisdn_add_range(
    prefix: Annotated[str, typer.Argument(help="Numeric prefix, 4–7 digits, e.g. 9100")],
    count: Annotated[int, typer.Argument(help="Numbers to add (1..10000)")],
) -> None:
    """v0.17 — bulk-extend the MSISDN pool by ``count`` numbers starting
    at ``{prefix}{0:04d}``. Idempotent on overlap (existing rows are
    preserved). Operator-only.
    """

    async def _do() -> None:
        out = await get_clients().inventory.add_msisdn_range(prefix, count)
        rprint(
            f"[green]inserted[/] {out.get('inserted')} / [dim]skipped[/] "
            f"{out.get('skipped')}  ({out.get('first')} … {out.get('last')})"
        )

    _run_safely(_do())


# ── eSIM ────────────────────────────────────────────────────────────

@esim_app.command("list")
def esim_list(
    state: Annotated[
        str | None,
        typer.Option("--state", help="available | reserved | activated | recycled"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
) -> None:
    """List eSIM profiles in the pool."""

    async def _do() -> None:
        rows = await get_clients().inventory.list_esims(state=state, limit=limit)
        if not rows:
            rprint("[yellow]no eSIM profiles match[/]")
            return
        table = Table(title=f"eSIM pool ({len(rows)} shown)")
        table.add_column("iccid")
        table.add_column("state")
        table.add_column("msisdn")
        for r in rows:
            table.add_row(
                r.get("iccid", "?"),
                r.get("profile_state", r.get("status", r.get("state", "?"))),
                r.get("assigned_msisdn", r.get("msisdn")) or "—",
            )
        rprint(table)

    _run_safely(_do())


@esim_app.command("show")
def esim_show(iccid: Annotated[str, typer.Argument(help="ICCID")]) -> None:
    """Show one eSIM profile (JSON)."""

    async def _do() -> None:
        rprint(await get_clients().inventory.get_esim(iccid))

    _run_safely(_do())


@esim_app.command("activation")
def esim_activation(iccid: Annotated[str, typer.Argument(help="ICCID")]) -> None:
    """Show the LPA activation code + IMSI for an eSIM."""

    async def _do() -> None:
        rprint(await get_clients().inventory.get_activation_code(iccid))

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
