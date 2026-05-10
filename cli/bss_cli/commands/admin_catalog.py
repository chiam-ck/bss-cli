"""`bss admin catalog ...` — operator catalog management.

CLI-only by design. The LLM tool surface does NOT expose these — catalog
edits are an operator task that should be deliberate and audited, not
part of a chat. Every command is a thin Typer wrapper over the
service-layer methods via ``bss-clients``; no raw SQL.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_clock import now as clock_now
from bss_orchestrator.clients import get_clients
from rich import print as rprint
from rich.table import Table

from .._runtime import run_async

app = typer.Typer(
    help="Operator catalog management (offerings, prices, windows, migrations).",
    no_args_is_help=True,
)


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        rprint(f"[red]Invalid ISO-8601 datetime: {value!r}[/]")
        raise typer.Exit(code=2)


@app.command("add-offering")
def add_offering(
    offering_id: Annotated[str, typer.Option("--id", help="Offering ID, e.g. PLAN_XS.")],
    name: Annotated[str, typer.Option("--name")],
    price: Annotated[str, typer.Option("--price", help="Recurring price amount.")],
    currency: Annotated[str, typer.Option("--currency")] = "SGD",
    valid_from: Annotated[str | None, typer.Option("--valid-from")] = None,
    valid_to: Annotated[str | None, typer.Option("--valid-to")] = None,
    data_mb: Annotated[int | None, typer.Option("--data-mb")] = None,
    voice_min: Annotated[int | None, typer.Option("--voice-min")] = None,
    sms_count: Annotated[int | None, typer.Option("--sms-count")] = None,
    data_roaming_mb: Annotated[
        int | None,
        typer.Option(
            "--data-roaming-mb",
            help="Roaming data allowance in MB. v0.17+ first-class allowance type. "
                 "0 is permitted (plan with no included roaming; customer can still "
                 "top up via VAS_ROAMING_*).",
        ),
    ] = None,
) -> None:
    """Add a new product offering with its recurring price + bundle allowances."""
    amount = str(Decimal(price))

    async def _do() -> None:
        result = await get_clients().catalog.admin_add_offering(
            offering_id=offering_id,
            name=name,
            amount=amount,
            currency=currency,
            valid_from=_parse_iso(valid_from),
            valid_to=_parse_iso(valid_to),
            data_mb=data_mb,
            voice_minutes=voice_min,
            sms_count=sms_count,
            data_roaming_mb=data_roaming_mb,
        )
        rprint(f"[green]✓[/] Added offering [bold]{result['id']}[/] — {result['name']}")

    _run_safely(_do())


@app.command("set-price")
def set_price(
    offering: Annotated[str, typer.Option("--offering", help="Offering ID.")],
    amount: Annotated[str, typer.Option("--amount")],
    valid_from: Annotated[str, typer.Option("--valid-from")],
    valid_to: Annotated[str | None, typer.Option("--valid-to")] = None,
    currency: Annotated[str, typer.Option("--currency")] = "SGD",
    price_id: Annotated[
        str | None,
        typer.Option("--price-id", help="Override generated PRICE_<offering>_<ts>."),
    ] = None,
    retire_current: Annotated[
        bool,
        typer.Option(
            "--retire-current",
            help="Stamp valid_to on the current active row(s) so the new row takes over.",
        ),
    ] = False,
) -> None:
    """Insert a new product_offering_price row, optionally retiring the current."""
    parsed_amount = str(Decimal(amount))

    async def _do() -> None:
        resolved_id = price_id or f"PRICE_{offering}_{int(clock_now().timestamp())}"
        result = await get_clients().catalog.admin_add_price(
            offering,
            price_id=resolved_id,
            amount=parsed_amount,
            currency=currency,
            valid_from=_parse_iso(valid_from),
            valid_to=_parse_iso(valid_to),
            retire_current=retire_current,
        )
        rprint(
            f"[green]✓[/] Added price [bold]{result['id']}[/] for {offering}: "
            f"{currency} {parsed_amount}"
        )

    _run_safely(_do())


@app.command("window-offering")
def window_offering(
    offering_id: Annotated[str, typer.Option("--id")],
    valid_from: Annotated[str | None, typer.Option("--valid-from")] = None,
    valid_to: Annotated[str | None, typer.Option("--valid-to")] = None,
) -> None:
    """Set valid_from/valid_to on an existing offering. Use to retire or to launch a promo."""

    async def _do() -> None:
        result = await get_clients().catalog.admin_set_offering_window(
            offering_id,
            valid_from=_parse_iso(valid_from),
            valid_to=_parse_iso(valid_to),
        )
        rprint(
            f"[green]✓[/] Windowed [bold]{result['id']}[/]: "
            f"valid_from={valid_from or 'NULL'}, valid_to={valid_to or 'NULL'}"
        )

    _run_safely(_do())


@app.command("migrate-price")
def migrate_price(
    offering: Annotated[str, typer.Option("--offering")],
    new_price_id: Annotated[str, typer.Option("--new-price-id")],
    effective_from: Annotated[str, typer.Option("--effective-from")],
    notice_days: Annotated[int, typer.Option("--notice-days")] = 30,
    initiated_by: Annotated[
        str,
        typer.Option("--initiated-by", help="Operator id stamped into the audit trail."),
    ] = "ops",
) -> None:
    """Migrate every active subscription on `--offering` to `--new-price-id` with notice."""

    async def _do() -> None:
        result = await get_clients().subscription.migrate_to_new_price(
            offering_id=offering,
            new_price_id=new_price_id,
            effective_from=_parse_iso(effective_from),
            notice_days=notice_days,
            initiated_by=initiated_by,
        )
        rprint(
            f"[green]✓[/] Scheduled price migration: "
            f"[bold]{result['count']}[/] subscription(s) on {offering}"
        )
        for sub_id in result.get("subscriptionIds", []):
            rprint(f"  • {sub_id}")

    _run_safely(_do())


@app.command("show")
def show(
    at: Annotated[
        str | None,
        typer.Option("--at", help="ISO-8601 moment; defaults to now."),
    ] = None,
) -> None:
    """Render the active catalog at a given moment."""

    async def _do() -> None:
        moment = _parse_iso(at) if at else None
        offerings = await get_clients().catalog.list_active_offerings(at=moment)
        table = Table(title=f"Active catalog @ {(moment or clock_now()).isoformat()}")
        table.add_column("offering")
        table.add_column("name")
        table.add_column("price")
        table.add_column("valid_from")
        table.add_column("valid_to")
        for o in offerings:
            prices = o.get("productOfferingPrice", [])
            recurring = next(
                (p for p in prices if p.get("priceType") == "recurring"), None
            )
            price_str = "—"
            if recurring:
                tx = recurring.get("price", {}).get("taxIncludedAmount", {})
                price_str = f"{tx.get('unit', 'SGD')} {tx.get('value', '?')}"
            valid_for = o.get("validFor") or {}
            table.add_row(
                o.get("id", ""),
                o.get("name", ""),
                price_str,
                str(valid_for.get("startDateTime") or "NULL"),
                str(valid_for.get("endDateTime") or "NULL"),
            )
        rprint(table)

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
