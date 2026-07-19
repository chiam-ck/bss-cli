"""`bss subscription ...` — subscription + VAS commands."""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_cockpit.renderers import render_esim_activation, render_subscription
from bss_orchestrator.clients import get_clients
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Manage subscriptions + VAS.", no_args_is_help=True)


@app.command("show")
def show(
    subscription_id: Annotated[str, typer.Argument()],
    show_esim: Annotated[bool, typer.Option("--show-esim")] = False,
) -> None:
    """Render a subscription (bundle bars + state). With --show-esim, append
    the eSIM activation card."""

    async def _do() -> None:
        c = get_clients()
        sub = await c.subscription.get(subscription_id)
        offering = None
        try:
            offering = await c.catalog.get_offering(sub["offeringId"])
        except Exception:
            pass
        customer = None
        try:
            customer = await c.crm.get_customer(sub["customerId"])
        except Exception:
            pass
        print(render_subscription(sub, customer=customer, offering=offering))
        if show_esim:
            act = await c.subscription.get_esim_activation(subscription_id)
            print(render_esim_activation(act))

    _run_safely(_do())


@app.command("list")
def list_(customer: Annotated[str, typer.Option("--customer")]) -> None:
    """List subscriptions for a customer."""

    async def _do() -> None:
        c = get_clients()
        for s in await c.subscription.list_for_customer(customer):
            rprint(
                f"{s['id']:<8}  {s.get('offeringId', ''):<8} "
                f"{s.get('state', ''):<8} MSISDN {s.get('msisdn', '')}"
            )

    _run_safely(_do())


@app.command("vas")
def vas(
    subscription_id: Annotated[str, typer.Argument()],
    vas_offering_id: Annotated[str, typer.Argument(help="e.g. VAS_DATA_5GB")],
) -> None:
    """Purchase a VAS top-up for a subscription (charged to COF)."""

    async def _do() -> None:
        c = get_clients()
        out = await c.subscription.purchase_vas(subscription_id, vas_offering_id)
        rprint(f"[green]{out['id']}[/] → {out.get('state')}  (+{vas_offering_id})")

    _run_safely(_do())


@app.command("renew")
def renew(subscription_id: Annotated[str, typer.Argument()]) -> None:
    """Manually renew a subscription (normally automatic at period boundary)."""

    async def _do() -> None:
        c = get_clients()
        out = await c.subscription.renew(subscription_id)
        rprint(f"[green]{out['id']}[/] renewed → next {out.get('nextRenewalAt')}")

    _run_safely(_do())


@app.command("terminate")
def terminate(
    subscription_id: Annotated[str, typer.Argument()],
    allow_destructive: Annotated[bool, typer.Option("--allow-destructive")] = False,
) -> None:
    """Terminate a subscription (destructive)."""
    if not allow_destructive:
        rprint("[yellow]terminate is gated behind --allow-destructive.[/]")
        raise typer.Exit(code=2)

    async def _do() -> None:
        c = get_clients()
        out = await c.subscription.terminate(subscription_id)
        rprint(f"[green]{out['id']}[/] → {out['state']}")

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
