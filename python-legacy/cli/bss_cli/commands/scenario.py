"""`bss scenario ...` — YAML scenario runner.

Three subcommands:

* ``bss scenario validate <path>...`` — parse every file, report errors.
* ``bss scenario list <dir>`` — list scenarios with tags + step count.
* ``bss scenario run <path>``  — execute one scenario.
    * ``--no-llm``  → fail ``ask:`` steps with a clear message.
    * ``--via-llm`` → (Task #6) force every step through the LLM.

All three honour the repo-wide ``bss-clients`` channel context: scenario
runs set ``X-BSS-Channel: scenario`` so downstream interaction logs
reflect that the action originated from a scenario, not a human CSR.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich import print as rprint
from rich.table import Table

from .._runtime import run_async
from ..scenarios import load_scenario, run_scenario
from ..scenarios.reporting import render_result, render_summary
from ..scenarios.schema import LLMMode

app = typer.Typer(help="YAML scenario runner.", no_args_is_help=True)


@app.command("validate")
def validate(
    paths: Annotated[list[Path], typer.Argument(help="YAML file(s) to validate.")],
) -> None:
    """Parse each scenario file; exit non-zero if any fail."""
    failed = 0
    for path in paths:
        try:
            scenario = load_scenario(path)
            rprint(
                f"[green]✓[/] {path} — {scenario.name} "
                f"([dim]{len(scenario.steps)} steps[/])"
            )
        except (ValidationError, ValueError) as e:
            failed += 1
            rprint(f"[red]✗[/] {path}")
            rprint(f"  [red]{e}[/]")
    if failed:
        raise typer.Exit(code=1)


@app.command("list")
def list_scenarios(
    directory: Annotated[
        Path,
        typer.Argument(help="Directory of scenario YAML files."),
    ] = Path("scenarios"),
) -> None:
    """List scenarios in ``directory`` with tags and step count."""
    if not directory.is_dir():
        rprint(f"[red]not a directory:[/] {directory}")
        raise typer.Exit(code=2)

    table = Table(title=f"scenarios in {directory}")
    table.add_column("file")
    table.add_column("name")
    table.add_column("tags")
    table.add_column("steps", justify="right")
    files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    if not files:
        rprint(f"[yellow]no YAML files in {directory}[/]")
        return
    for path in files:
        try:
            s = load_scenario(path)
            table.add_row(path.name, s.name, ", ".join(s.tags) or "—", str(len(s.steps)))
        except Exception as e:
            table.add_row(path.name, "[red]INVALID[/]", "", str(e)[:60])
    rprint(table)


@app.command("run")
def run(
    path: Annotated[Path, typer.Argument(help="Scenario YAML file.")],
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Fail ask: steps immediately.")
    ] = False,
    via_llm: Annotated[
        bool,
        typer.Option("--via-llm", help="[Task #6] Force every step through the LLM."),
    ] = False,
) -> None:
    """Run a single scenario file."""
    if no_llm and via_llm:
        rprint("[red]--no-llm and --via-llm are mutually exclusive.[/]")
        raise typer.Exit(code=2)

    mode: LLMMode = "auto"
    if no_llm:
        mode = "disabled"
    elif via_llm:
        mode = "forced"

    try:
        scenario = load_scenario(path)
    except (ValidationError, ValueError) as e:
        rprint(f"[red]invalid scenario {path}[/]")
        rprint(f"  [red]{e}[/]")
        raise typer.Exit(code=2)

    result = run_async(run_scenario(scenario, mode=mode))
    render_result(result)
    if not result.ok:
        raise typer.Exit(code=1)


@app.command("run-all")
def run_all(
    directory: Annotated[
        Path,
        typer.Argument(help="Directory of scenario YAML files."),
    ] = Path("scenarios"),
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Skip ``ask:`` steps — fail fast."),
    ] = False,
    tag: Annotated[
        str | None,
        typer.Option("--tag", help="Only run scenarios tagged with this value."),
    ] = None,
) -> None:
    """Run every scenario in ``directory`` (alphabetical) and print a summary.

    Exit code is non-zero if any scenario fails. Individual scenario
    output is rendered in full order; a summary table prints at the end.
    """
    if not directory.is_dir():
        rprint(f"[red]not a directory:[/] {directory}")
        raise typer.Exit(code=2)

    mode: LLMMode = "disabled" if no_llm else "auto"
    files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    if not files:
        rprint(f"[yellow]no YAML files in {directory}[/]")
        return

    results = []
    for path in files:
        try:
            scenario = load_scenario(path)
        except (ValidationError, ValueError) as e:
            rprint(f"[red]invalid scenario {path}:[/] {e}")
            raise typer.Exit(code=2)
        if tag is not None and tag not in scenario.tags:
            continue
        result = run_async(run_scenario(scenario, mode=mode))
        render_result(result)
        results.append(result)

    if not results:
        rprint(f"[yellow]no scenarios matched tag={tag!r}[/]")
        return

    render_summary(results)
    if any(not r.ok for r in results):
        raise typer.Exit(code=1)
