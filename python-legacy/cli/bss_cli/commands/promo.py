"""`bss promo ...` — operator promotion management (v1.1).

Thin Typer wrappers over the catalog promo surface (which composes over
loyalty-cli). Operator-only by design — these are NOT in the
``customer_self_serve`` tool profile. A customer types a code at checkout;
they never create or assign promotions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer
from bss_clients import NotFound, PolicyViolationFromServer
from bss_orchestrator.clients import get_clients
from rich import print as rprint
from rich.table import Table

from .._runtime import run_async

app = typer.Typer(
    help="Operator promotion management (create / assign / show). Composes over loyalty-cli.",
    no_args_is_help=True,
)


@app.command("create")
def create(
    promotion_id: Annotated[str, typer.Option("--id", help="Promotion id, e.g. PROMO_SUMMER25.")],
    discount_type: Annotated[str, typer.Option("--type", help="percent | absolute.")],
    discount_value: Annotated[str, typer.Option("--value", help="Discount amount (percent 0-100, or absolute).")],
    duration_kind: Annotated[str, typer.Option("--duration", help="single | multi | perpetual.")],
    audience: Annotated[
        str,
        typer.Option("--audience", help="public (typed) | targeted (eligibility-gated, auto-applied)."),
    ] = "public",
    currency: Annotated[str, typer.Option("--currency")] = "SGD",
    code: Annotated[
        str | None,
        typer.Option("--code", help="Typed code (non-targeted). Omit for codeless targeted."),
    ] = None,
    promo_code_kind: Annotated[
        str | None,
        typer.Option(
            "--code-kind",
            help="single_use_shared | multi_use | single_use_unique_per_customer "
            "(required with --code).",
        ),
    ] = None,
    offerings: Annotated[
        str | None,
        typer.Option(
            "--offerings",
            help="Comma-separated offering ids to restrict to. Omit = all sellable.",
        ),
    ] = None,
    periods_total: Annotated[
        int | None,
        typer.Option("--periods", help="Number of periods (required for --duration multi, >= 2)."),
    ] = None,
    valid_from: Annotated[str | None, typer.Option("--valid-from")] = None,
    valid_to: Annotated[str | None, typer.Option("--valid-to")] = None,
    display_name: Annotated[str | None, typer.Option("--name")] = None,
) -> None:
    """Create a promotion (BSS money terms + loyalty entitlement saga)."""
    value = str(Decimal(discount_value))
    offering_ids = [o.strip() for o in offerings.split(",")] if offerings else None

    async def _do() -> None:
        result = await get_clients().catalog.create_promotion(
            promotion_id=promotion_id,
            discount_type=discount_type,
            discount_value=value,
            duration_kind=duration_kind,
            audience=audience,
            currency=currency,
            code=code,
            promo_code_kind=promo_code_kind,
            applicable_offering_ids=offering_ids,
            periods_total=periods_total,
            valid_from=valid_from,
            valid_to=valid_to,
            display_name=display_name,
        )
        rprint(
            f"[green]✓[/] Created promotion [bold]{result['id']}[/] "
            f"(audience={result.get('audience')}, code={result.get('code')}) — "
            f"state={result['state']}, OD={result.get('offerDefinitionId')}"
        )

    _run_safely(_do())


@app.command("assign")
def assign(
    promotion_id: Annotated[str, typer.Option("--promo", help="An active promotion id.")],
    customers: Annotated[str, typer.Option("--customers", help="Comma-separated customer ids.")],
) -> None:
    """Add customers to a targeted promotion's eligibility list."""
    customer_ids = [c.strip() for c in customers.split(",") if c.strip()]

    async def _do() -> None:
        result = await get_clients().catalog.assign_promotion(
            promotion_id, customer_ids=customer_ids
        )
        eligible = result.get("eligible", [])
        already = result.get("already", [])
        rprint(
            f"[green]✓[/] Eligibility for [bold]{promotion_id}[/] "
            f"(code {result.get('code')}): {len(eligible)} added, {len(already)} already"
        )
        for cid in eligible:
            rprint(f"  • [green]added[/] {cid}")
        for cid in already:
            rprint(f"  • [dim]already[/] {cid}")

    _run_safely(_do())


@app.command("unassign")
def unassign(
    promotion_id: Annotated[str, typer.Option("--promo", help="An active targeted promotion id.")],
    customers: Annotated[str, typer.Option("--customers", help="Comma-separated customer ids.")],
) -> None:
    """Remove customers from a targeted promotion's eligibility list (v1.3.1).

    Mirrors ``assign``: drops the BSS eligibility row AND ``offer.revoke``s the
    upfront-minted loyalty offer so loyalty's per-customer views stop showing
    the customer as paired. Idempotent — a customer not on the list is reported
    under ``not_eligible``. A loyalty revoke failure logs a drift warning but
    does not block the BSS-side removal.
    """
    customer_ids = [c.strip() for c in customers.split(",") if c.strip()]

    async def _do() -> None:
        result = await get_clients().catalog.unassign_promotion(
            promotion_id, customer_ids=customer_ids
        )
        removed = result.get("removed", [])
        not_eligible = result.get("not_eligible", [])
        rprint(
            f"[green]✓[/] Removed from [bold]{promotion_id}[/] "
            f"(code {result.get('code')}): {len(removed)} removed, "
            f"{len(not_eligible)} not eligible"
        )
        for cid in removed:
            rprint(f"  • [yellow]removed[/] {cid}")
        for cid in not_eligible:
            rprint(f"  • [dim]not_eligible[/] {cid}")

    _run_safely(_do())


@app.command("exhaust")
def exhaust(
    promotion_id: Annotated[str, typer.Argument(help="Promotion id to exhaust.")],
) -> None:
    """Terminal-stop a promotion (v1.4.1). Flips ``active → exhausted``;
    new orders see no discount and proceed at full price. The row stays
    for audit; no archive. Idempotent — re-running on an already-exhausted
    promo is a no-op.

    Use this when an operator decides a promo is over but the row should
    remain visible for reporting (e.g. campaign ended, fraud-driven kill,
    one-off close after manual review)."""
    async def _do() -> None:
        promo = await get_clients().catalog.exhaust_promotion(promotion_id)
        state = promo.get("state", "?")
        if state == "exhausted":
            rprint(
                f"[green]✓[/] [bold]{promotion_id}[/] is now [yellow]exhausted[/] "
                f"(was active). New orders will see no discount."
            )
        else:
            rprint(
                f"[yellow]![/] [bold]{promotion_id}[/] state: {state} "
                f"(unexpected; check `bss promo show`)"
            )

    _run_safely(_do())


@app.command("show")
def show(
    promotion_id: Annotated[str, typer.Argument(help="Promotion id.")],
) -> None:
    """Show a promotion's money terms, loyalty link, and state."""

    async def _do() -> None:
        p = await get_clients().catalog.get_promotion(promotion_id)
        table = Table(title=f"Promotion {p['id']}")
        table.add_column("field")
        table.add_column("value")
        table.add_row("name", str(p.get("name") or "—"))
        table.add_row("state", p.get("state", ""))
        table.add_row("audience", str(p.get("audience") or "—"))
        table.add_row("code", str(p.get("code") or "—"))
        table.add_row("offerDefinitionId", str(p.get("offerDefinitionId") or "—"))
        table.add_row("discount", f"{p.get('discountType')} {p.get('discountValue')}")
        table.add_row("duration", f"{p.get('durationKind')} (periods={p.get('periodsTotal') or '—'})")
        table.add_row("applicableOfferings", str(p.get("applicableOfferingIds") or "all"))
        table.add_row("validFrom", str(p.get("validFrom") or "—"))
        table.add_row("validTo", str(p.get("validTo") or "—"))
        rprint(table)

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
    except NotFound as e:
        rprint(f"[red]NOT_FOUND[/]  {e.detail}")
        raise typer.Exit(code=2)
