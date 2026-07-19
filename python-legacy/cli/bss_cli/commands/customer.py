"""`bss customer ...` — direct customer commands."""

from __future__ import annotations

from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_cockpit.renderers import render_customer_360
from bss_orchestrator.clients import get_clients
from bss_orchestrator.tools.payment import local_tokenize_card
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Manage customers (TMF629).", no_args_is_help=True)


@app.command("create")
def create(
    name: Annotated[str, typer.Option("--name", help="Customer display name.")],
    email: Annotated[str | None, typer.Option("--email")] = None,
    phone: Annotated[str | None, typer.Option("--phone")] = None,
    card: Annotated[
        str | None,
        typer.Option("--card", help="16-digit PAN; CLI tokenises it client-side (sandbox)."),
    ] = None,
) -> None:
    """Create a customer; optionally tokenise + attach a card-on-file."""

    async def _do() -> None:
        c = get_clients()
        customer = await c.crm.create_customer(name=name, email=email, phone=phone)
        ind = customer.get("individual") or {}
        display = " ".join(p for p in (ind.get("givenName"), ind.get("familyName")) if p) or "—"
        rprint(f"[green]Created[/] {customer['id']}  {display}")
        if card:
            tok = local_tokenize_card(card)
            pm = await c.payment.create_payment_method(
                customer_id=customer["id"],
                card_token=tok["cardToken"],
                last4=tok["last4"],
                brand=tok["brand"],
            )
            cs = pm.get("cardSummary") or {}
            rprint(f"[green]Attached card[/] {pm['id']}  {cs.get('brand', '')}•••{cs.get('last4', '')}")

    _run_safely(_do())


@app.command("list")
def list_(
    state: Annotated[str | None, typer.Option("--state")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Filter by name substring.")] = None,
) -> None:
    """List customers, optionally filtered by state or name substring."""

    async def _do() -> None:
        c = get_clients()
        rows = await c.crm.list_customers(state=state, name_contains=name)
        for r in rows:
            # TMF629 emits retail name as ``individual.{givenName,familyName}``.
            # Fall back to a flat ``individual.name`` (not emitted today but
            # permitted by the spec) and finally to top-level ``name``.
            ind = r.get("individual") or {}
            parts = [ind.get("givenName"), ind.get("familyName")]
            full = " ".join(p for p in parts if p).strip()
            display_name = full or ind.get("name") or r.get("name") or "—"
            rprint(
                f"{r['id']:<15}  {display_name:<30} "
                f"{r.get('status', r.get('state', ''))}"
            )

    _run_safely(_do())


@app.command("show")
def show(
    customer_id: Annotated[str, typer.Argument(help="Customer ID (CUST-NNN).")],
) -> None:
    """Render the customer 360 view."""

    async def _do() -> None:
        c = get_clients()
        cust = await c.crm.get_customer(customer_id)
        subs = await c.subscription.list_for_customer(customer_id)
        cases = await c.crm.list_cases(customer_id=customer_id)
        tickets_by_case: dict[str, list] = {}
        for case in cases:
            tickets_by_case[case["id"]] = await c.crm.list_tickets(case_id=case["id"])
        interactions = await c.crm.list_interactions(customer_id, limit=10)
        print(
            render_customer_360(
                cust,
                subscriptions=subs,
                cases=cases,
                tickets_by_case=tickets_by_case,
                interactions=interactions,
            )
        )

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
