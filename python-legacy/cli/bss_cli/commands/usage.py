"""`bss usage ...` — mediation / usage simulation commands."""

from __future__ import annotations

import re
from typing import Annotated

import typer
from bss_clients import PolicyViolationFromServer
from bss_clock import now as clock_now
from bss_orchestrator.clients import get_clients
from rich import print as rprint

from .._runtime import run_async

app = typer.Typer(help="Usage simulation (TMF635 mediation).", no_args_is_help=True)

_QUANTITY_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]+)$")


def _parse_quantity(raw: str, event_type: str) -> tuple[int, str]:
    """Parse e.g. '1GB' → (1024, 'mb') or '500MB' → (500, 'mb')."""
    m = _QUANTITY_RE.match(raw.strip())
    if not m:
        # fall back to plain int
        return int(float(raw)), _default_unit(event_type)
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "gb":
        return int(value * 1024), "mb"
    if unit == "mb":
        return int(value), "mb"
    if unit in ("min", "mins", "minutes"):
        return int(value), "minutes"
    if unit in ("sms", "count", "ct"):
        return int(value), "count"
    return int(value), unit


def _default_unit(event_type: str) -> str:
    return {"data": "mb", "voice_minutes": "minutes", "sms": "count"}.get(event_type, "count")


@app.command("simulate")
def simulate(
    msisdn: Annotated[str, typer.Option("--msisdn")],
    type_: Annotated[str, typer.Option("--type", help="data | voice_minutes | sms")] = "data",
    quantity: Annotated[str, typer.Option("--quantity", help="e.g. 1GB, 500MB, 5, 3min")] = "1",
) -> None:
    """Submit a single usage event to mediation."""

    async def _do() -> None:
        c = get_clients()
        qty, unit = _parse_quantity(quantity, type_)
        now = clock_now().replace(microsecond=0).isoformat()
        out = await c.mediation.submit_usage(
            msisdn=msisdn,
            event_type=type_,
            event_time=now,
            quantity=qty,
            unit=unit,
            source="cli",
        )
        state = "processed" if out.get("processed") else "rejected"
        rprint(f"[green]{out['id']}[/]  {state}  msisdn={msisdn} {qty}{unit}")

    _run_safely(_do())


def _run_safely(coro) -> None:
    try:
        run_async(coro)
    except PolicyViolationFromServer as e:
        rprint(f"[red]POLICY_VIOLATION[/] [bold]{e.rule}[/]  {e.detail}")
        raise typer.Exit(code=2)
