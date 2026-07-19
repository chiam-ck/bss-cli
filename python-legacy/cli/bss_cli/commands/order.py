"""`bss order ...` — commercial order commands."""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_cockpit.renderers import render_order
from bss_orchestrator.clients import get_clients
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Manage commercial orders (TMF622).", no_args_is_help=True)


@app.command("create")
def create(
    customer: Annotated[str, typer.Option("--customer")],
    offering: Annotated[str, typer.Option("--offering", help="Offering ID — see `bss catalog list`.")],
    msisdn: Annotated[str | None, typer.Option("--msisdn", help="Preferred MSISDN.")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
) -> None:
    """Create and submit a commercial order. Optionally wait for completion."""

    async def _do() -> None:
        c = get_clients()
        o = await c.com.create_order(
            customer_id=customer,
            offering_id=offering,
            msisdn_preference=msisdn,
        )
        rprint(f"[green]Created[/] {o['id']}  {offering}  [{o['state']}]")
        o = await c.com.submit_order(o["id"])
        rprint(f"[green]Submitted[/] {o['id']}  [{o['state']}]")
        if wait:
            o = await c.com.wait_until(o["id"], target_state="completed", timeout_s=30)
            rprint(f"[green]Final[/] {o['id']}  [{o['state']}]")

    _run_safely(_do())


@app.command("show")
def show(order_id: Annotated[str, typer.Argument()]) -> None:
    """Render an order with its SOM decomposition tree."""

    async def _do() -> None:
        c = get_clients()
        o = await c.com.get_order(order_id)
        service_orders = await c.som.list_for_order(order_id)
        services_by_so: dict[str, list] = {}
        tasks_by_service: dict[str, list] = {}
        for so in service_orders:
            # Services in this phase are reached via subscription lookup;
            # when order is done the subscription_id is in the item.
            pass
        sub_id = None
        for item in o.get("items", []):
            sub_id = item.get("targetSubscriptionId") or sub_id
        if sub_id:
            services = await c.som.list_services_for_subscription(sub_id)
            # Naive: attribute all services to the first service-order
            if service_orders:
                services_by_so[service_orders[0]["id"]] = services
            for svc in services:
                tasks_by_service[svc["id"]] = await c.provisioning.list_tasks(
                    service_id=svc["id"]
                )
        print(
            render_order(
                o,
                service_orders=service_orders,
                services_by_so=services_by_so,
                tasks_by_service=tasks_by_service,
                subscription_id=sub_id,
            )
        )

    _run_safely(_do())


@app.command("list")
def list_(
    customer: Annotated[str, typer.Option("--customer")],
) -> None:
    """List orders for a customer."""

    async def _do() -> None:
        c = get_clients()
        for o in await c.com.list_orders(customer):
            items = o.get("items", [])
            offer = items[0].get("offeringId") if items else "—"
            rprint(f"{o['id']:<9}  {offer:<8}  [{o.get('state')}]  {o.get('orderDate', '')}")

    _run_safely(_do())


@app.command("cancel")
def cancel(
    order_id: Annotated[str, typer.Argument()],
    allow_destructive: Annotated[bool, typer.Option("--allow-destructive")] = False,
) -> None:
    """Cancel an order (destructive)."""
    if not allow_destructive:
        rprint("[yellow]cancel is gated behind --allow-destructive.[/]")
        raise typer.Exit(code=2)

    async def _do() -> None:
        c = get_clients()
        o = await c.com.cancel_order(order_id)
        rprint(f"[green]{o['id']}[/] → {o['state']}")

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
