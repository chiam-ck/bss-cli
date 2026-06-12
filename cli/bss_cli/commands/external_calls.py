"""``bss external-calls`` — read-only browser over the integrations
forensic substrate (v0.14+).

Surfaces ``integrations.external_call`` rows for triage. Typical
operator queries:

* ``bss external-calls`` — last 50 across all providers.
* ``bss external-calls --provider resend`` — filter by provider.
* ``bss external-calls --since 1h`` — last hour.
* ``bss external-calls --aggregate IDT-0042`` — every call against
  one identity (forensic correlation).
* ``bss external-calls --month-to-date`` — call count for free-tier
  monitoring (Resend 3k/mo, Didit 500/mo, Stripe per-charge).

Read-only by design. The CLI never inserts or updates external_call
rows — that's the adapter layer's job.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Annotated

import typer
from bss_clock import now as clock_now
from rich.console import Console
from rich.table import Table

from .._runtime import run_async

app = typer.Typer(
    name="external-calls",
    help="Read-only browser over integrations.external_call.",
    no_args_is_help=False,
    invoke_without_command=True,
)

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")


@app.callback()
def list_calls(
    ctx: typer.Context,
    provider: Annotated[
        str | None,
        typer.Option("--provider", "-p", help="Filter by provider name."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "Time window relative to now: 30s | 5m | 1h | 24h | 7d. "
                "Conflicts with --month-to-date."
            ),
        ),
    ] = None,
    aggregate: Annotated[
        str | None,
        typer.Option(
            "--aggregate",
            "-a",
            help="Filter by aggregate id (e.g. IDT-0042).",
        ),
    ] = None,
    month_to_date: Annotated[
        bool,
        typer.Option(
            "--month-to-date",
            help=(
                "Show count for the current calendar month. Useful for "
                "free-tier monitoring (Didit 500/mo, Resend 3k/mo)."
            ),
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit", "-n",
            help="Max rows to display (default 50).",
        ),
    ] = 50,
    failures_only: Annotated[
        bool,
        typer.Option(
            "--failures",
            help="Show only success=false rows.",
        ),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    if month_to_date and since:
        typer.echo("--month-to-date and --since are mutually exclusive", err=True)
        raise typer.Exit(code=2)

    since_dt: datetime | None = None
    if month_to_date:
        now = clock_now()
        since_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif since:
        since_dt = _parse_since(since)

    run_async(
        _run_query(
            provider=provider,
            since=since_dt,
            aggregate=aggregate,
            limit=limit,
            failures_only=failures_only,
            month_to_date_summary=month_to_date,
        )
    )


def _parse_since(spec: str) -> datetime:
    """Parse '30s' / '5m' / '1h' / '24h' / '7d' into a datetime in the past."""
    m = _DURATION_RE.match(spec.strip())
    if not m:
        typer.echo(
            f"--since {spec!r} not parseable; expected '<n>{{s,m,h,d}}' "
            "e.g. '30m', '24h', '7d'",
            err=True,
        )
        raise typer.Exit(code=2)
    n = int(m.group(1))
    unit = m.group(2)
    delta = {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]
    return clock_now() - delta


async def _run_query(
    *,
    provider: str | None,
    since: datetime | None,
    aggregate: str | None,
    limit: int,
    failures_only: bool,
    month_to_date_summary: bool,
) -> None:
    # Lazy imports — keep `bss --help` cheap; only pay the SQLAlchemy
    # cost when the user actually runs this command.
    import os

    from bss_models.integrations import ExternalCall
    from sqlalchemy import and_, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    db_url = os.environ.get("BSS_DB_URL")
    if not db_url:
        typer.echo("BSS_DB_URL not set; cannot query external_call.", err=True)
        raise typer.Exit(code=2)

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    filters = []
    if provider:
        filters.append(ExternalCall.provider == provider)
    if since:
        filters.append(ExternalCall.occurred_at >= since)
    if aggregate:
        filters.append(ExternalCall.aggregate_id == aggregate)
    if failures_only:
        filters.append(ExternalCall.success.is_(False))

    try:
        async with factory() as s:
            stmt = select(ExternalCall)
            if filters:
                stmt = stmt.where(and_(*filters))
            stmt = stmt.order_by(ExternalCall.occurred_at.desc()).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
    finally:
        await engine.dispose()

    console = Console()

    if month_to_date_summary:
        # Render an aggregate-by-provider summary instead of rows.
        by_provider: dict[str, int] = {}
        by_provider_failed: dict[str, int] = {}
        for r in rows:
            by_provider[r.provider] = by_provider.get(r.provider, 0) + 1
            if not r.success:
                by_provider_failed[r.provider] = by_provider_failed.get(r.provider, 0) + 1
        if not by_provider:
            console.print(
                "[yellow]No calls this calendar month.[/]"
            )
            return
        t = Table(
            title=f"External calls (month to date, ≤{limit} rows scanned)"
        )
        t.add_column("provider", style="green")
        t.add_column("calls", justify="right")
        t.add_column("failures", justify="right", style="red")
        for p in sorted(by_provider):
            t.add_row(
                p, str(by_provider[p]), str(by_provider_failed.get(p, 0))
            )
        console.print(t)
        return

    if not rows:
        console.print("[yellow]No matching calls.[/]")
        return

    t = Table(title=f"External calls (last {len(rows)})")
    t.add_column("when", style="dim")
    t.add_column("provider", style="green")
    t.add_column("op")
    t.add_column("ok", justify="center")
    t.add_column("ms", justify="right", style="dim")
    t.add_column("aggregate")
    t.add_column("call id", style="dim")
    t.add_column("error", style="red")

    for r in rows:
        ok = "[green]✓[/]" if r.success else "[red]✗[/]"
        when = r.occurred_at.astimezone(timezone.utc).strftime("%m-%d %H:%M:%S")
        agg = (
            f"{r.aggregate_type or ''}:{r.aggregate_id or ''}"
            if (r.aggregate_type or r.aggregate_id)
            else ""
        )
        t.add_row(
            when,
            r.provider,
            r.operation,
            ok,
            str(r.latency_ms),
            agg,
            r.provider_call_id or "",
            (r.error_message or r.error_code or "")[:40],
        )

    console.print(t)
