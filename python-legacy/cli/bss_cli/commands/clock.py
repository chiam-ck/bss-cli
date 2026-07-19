"""`bss clock ...` — time helpers.

v0.1 uses wall-clock time from ``datetime.now`` because a proper scenario
clock service doesn't exist yet. Phase 10 replaces these with calls to a
real clock service for deterministic scenarios.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import typer
from rich import print as rprint

app = typer.Typer(help="Time helpers (v0.1 = wall clock).", no_args_is_help=True)


@app.command("now")
def now() -> None:
    """Print the current time in ISO-8601 UTC."""
    rprint(datetime.now(timezone.utc).replace(microsecond=0).isoformat())


@app.command("advance")
def advance(duration: Annotated[str, typer.Argument(help="e.g. 30d, 1h, 15m")]) -> None:
    """(Phase 10) Advance the scenario clock. v0.1 prints a not-implemented notice."""
    rprint(
        "[yellow]clock.advance is a Phase 10 feature — "
        "scenario clock service not wired in v0.1.[/]\n"
        f"requested delta: {duration}"
    )
    raise typer.Exit(code=2)
