"""`bss payment ...` — payment-method + charge commands."""

from __future__ import annotations

import os
from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_orchestrator.clients import get_clients
from bss_orchestrator.tools.payment import local_tokenize_card
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Payment methods + payment attempts.", no_args_is_help=True)


@app.command("add-card")
def add_card(
    customer: Annotated[str, typer.Option("--customer")],
    card: Annotated[str, typer.Option("--card", help="16-digit PAN (tokenised by CLI).")],
) -> None:
    """Tokenise a PAN client-side and attach it as a payment method.

    v0.16+ — gated on ``BSS_PAYMENT_PROVIDER=mock``. In stripe-mode,
    customers add cards via the self-serve portal (Stripe Elements);
    server-side tokenization is forbidden in production because the
    PAN must never touch BSS. Sandbox CLI mirrors mock_tokenizer's
    FAIL/DECLINE embedding so charge-failure scenarios stay honest.
    """
    provider = os.environ.get("BSS_PAYMENT_PROVIDER", "mock")
    if provider != "mock":
        rprint(
            f"[red]ERROR[/] [bold]bss payment add-card[/] is dev-only and requires "
            f"[green]BSS_PAYMENT_PROVIDER=mock[/] (currently {provider!r}).\n"
            f"In stripe mode, customers add cards via the self-serve portal "
            f"([green]Stripe Elements[/]).\n"
            f"For test data, use the portal with Stripe test cards "
            f"(e.g. [green]4242 4242 4242 4242[/])."
        )
        raise typer.Exit(code=2)

    async def _do() -> None:
        c = get_clients()
        tok = local_tokenize_card(card)
        pm = await c.payment.create_payment_method(
            customer_id=customer,
            card_token=tok["cardToken"],
            last4=tok["last4"],
            brand=tok["brand"],
        )
        cs = pm.get("cardSummary") or {}
        rprint(f"[green]Added[/] {pm['id']}  {cs.get('brand', '')}•••{cs.get('last4', '')}")

    _run_safely(_do())


@app.command("list-methods")
def list_methods(customer: Annotated[str, typer.Option("--customer")]) -> None:
    """List payment methods for a customer."""

    async def _do() -> None:
        c = get_clients()
        for pm in await c.payment.list_methods(customer):
            cs = pm.get("cardSummary") or {}
            exp_m = cs.get("expMonth")
            exp_y = cs.get("expYear")
            exp = f"{exp_m:02}/{exp_y}" if exp_m and exp_y else "  /    "
            rprint(
                f"{pm['id']:<9} {cs.get('brand', ''):<6}•••{cs.get('last4', '')}  {exp}"
            )

    _run_safely(_do())


@app.command("remove-method")
def remove_method(
    method_id: Annotated[str, typer.Argument()],
    allow_destructive: Annotated[bool, typer.Option("--allow-destructive")] = False,
) -> None:
    """Remove a payment method (destructive)."""
    if not allow_destructive:
        rprint("[yellow]remove-method is gated behind --allow-destructive.[/]")
        raise typer.Exit(code=2)

    async def _do() -> None:
        c = get_clients()
        out = await c.payment.remove_method(method_id)
        rprint(f"[green]Removed[/] {out.get('id')}")

    _run_safely(_do())


@app.command("cutover")
def cutover(
    invalidate_mock_tokens: Annotated[
        bool,
        typer.Option(
            "--invalidate-mock-tokens",
            help="Mark every active mock-token payment method as expired.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the count without writing.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the y/N prompt (for scripts).",
        ),
    ] = False,
) -> None:
    """v0.16 cutover — switch from mock-tokens to a real provider.

    Run BEFORE flipping ``BSS_PAYMENT_PROVIDER=mock → stripe`` in
    production so the next charge against any pre-cutover saved card
    fails immediately and the customer is prompted to re-add via the
    portal — instead of silently failing weeks later.

    Each invalidated row emits a ``payment_method.cutover_invalidated``
    domain event so the v0.14 Resend email-template flow can notify
    each customer ("please update your payment method").
    """
    if not invalidate_mock_tokens:
        rprint(
            "[yellow]nothing to do[/] — pass [green]--invalidate-mock-tokens[/] "
            "to run the cutover (or [green]--dry-run[/] to preview)."
        )
        raise typer.Exit(code=0)

    async def _preview() -> dict:
        c = get_clients()
        return await c.payment.cutover_invalidate_mock_tokens(dry_run=True)

    async def _execute() -> dict:
        c = get_clients()
        return await c.payment.cutover_invalidate_mock_tokens(dry_run=False)

    # Always preview first so the operator sees the scope before
    # writing.
    preview = run_async(_preview())
    count = preview.get("candidate_count", 0)
    if count == 0:
        rprint("[green]No mock-token payment methods to invalidate.[/]")
        raise typer.Exit(code=0)

    rprint(
        f"[yellow]Found {count} active payment_methods with token_provider='mock'.[/]"
    )
    if dry_run:
        rprint("[green]Dry run — no writes performed.[/]")
        for pm_id in preview.get("candidate_ids", []):
            rprint(f"  would invalidate: {pm_id}")
        raise typer.Exit(code=0)

    if not yes:
        confirm = typer.confirm(
            "Mark all as expired? Customers will see "
            "'please update your payment method' on next attempt.",
            default=False,
        )
        if not confirm:
            rprint("[red]Aborted.[/]")
            raise typer.Exit(code=2)

    result = run_async(_execute())
    rprint(
        f"[green]Invalidated {result.get('invalidated_count', 0)} payment methods.[/]"
    )
    rprint(
        "[dim]Each row emitted a [green]payment_method.cutover_invalidated[/] "
        "event for the email-template flow to pick up.[/]"
    )


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
