"""Rich-based pass/fail rendering for scenario results.

Two surfaces:

* :func:`render_result` — one ``ScenarioResult`` with a header, a per-step
  table, and (on failure) the captured failure details. This is what
  ``bss scenario run`` prints.
* :func:`render_summary` — multi-scenario roll-up used by the future
  ``make scenarios-all`` entrypoint (post-task-#4).

Keep this module decoupled from typer — tests import it directly and
assert on rendered strings.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .runner import ScenarioResult, StepResult

_KIND_ICON = {"action": "▶", "assert": "=", "ask": "💬"}


def render_result(result: ScenarioResult, console: Console | None = None) -> None:
    console = console or Console()
    colour = "green" if result.ok else "red"
    status = "PASS" if result.ok else "FAIL"
    header = (
        f"[{colour}][bold]{status}[/] — {result.scenario}[/]  "
        f"[dim]({result.duration_ms:.0f} ms)[/]"
    )
    console.print(header)

    if result.setup_error:
        console.print(Panel(result.setup_error, title="setup error", border_style="red"))
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("kind", width=6)
    table.add_column("step")
    table.add_column("ms", justify="right")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for i, step in enumerate(result.steps, 1):
        table.add_row(
            str(i),
            f"{_KIND_ICON.get(step.kind, '?')} {step.kind}",
            step.name,
            f"{step.duration_ms:.0f}",
            _step_status(step),
            _step_detail(step),
        )
    console.print(table)

    if result.teardown_error:
        console.print(
            Panel(result.teardown_error, title="teardown error", border_style="yellow")
        )

    if not result.ok:
        _print_failure_context(result, console)
        # Ask steps may have PASSED but still be relevant to debugging a
        # later deterministic failure (e.g., LLM "fixed" nothing but the
        # fix wasn't durable). Surface their traces regardless.
        _print_ask_traces(result, console)


def _step_status(step: StepResult) -> Text:
    if step.ok:
        return Text("✓", style="green")
    return Text("✗", style="red")


def _step_detail(step: StepResult) -> str:
    if step.ok:
        bits: list[str] = []
        if step.captured:
            bits.append(
                ", ".join(f"{k}={_short(v)}" for k, v in step.captured.items())
            )
        # For ask steps, surface the tools the LLM actually invoked so a
        # green scenario still shows the ReAct trace at a glance and a
        # red one makes the gap between expected and actual obvious.
        if step.kind == "ask" and step.result is not None:
            tools = getattr(step.result, "tools_called", None)
            if tools:
                bits.append(f"tools: {', '.join(tools)}")
        return "  ".join(bits)
    return (step.error or "").strip().splitlines()[0] if step.error else "failed"


def _short(v: object, n: int = 40) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _print_ask_traces(result: ScenarioResult, console: Console) -> None:
    for step in result.steps:
        if step.kind != "ask" or step.result is None:
            continue
        tools = getattr(step.result, "tools_called", []) or []
        final_msg = getattr(step.result, "final_message", "") or ""
        if not (tools or final_msg):
            continue
        trace = []
        if tools:
            trace.append(f"tools called ({len(tools)}): {', '.join(tools)}")
        if final_msg:
            trace.append(f"\nfinal LLM message:\n{final_msg.strip()}")
        console.print(
            Panel("\n".join(trace), title=f"LLM trace — {step.name!r}", border_style="yellow")
        )


def _print_failure_context(result: ScenarioResult, console: Console) -> None:
    last = next((s for s in reversed(result.steps) if not s.ok), None)
    if last is None:
        return
    body = last.error or "(no detail captured)"
    console.print(
        Panel(body, title=f"failure in step {last.name!r}", border_style="red")
    )


def render_summary(results: list[ScenarioResult], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="scenario summary", show_header=True, header_style="bold")
    table.add_column("scenario")
    table.add_column("status")
    table.add_column("steps", justify="right")
    table.add_column("duration", justify="right")
    for r in results:
        status = Text("PASS", style="green") if r.ok else Text("FAIL", style="red")
        table.add_row(
            r.scenario,
            status,
            f"{sum(1 for s in r.steps if s.ok)}/{len(r.steps)}",
            f"{r.duration_ms:.0f} ms",
        )
    console.print(table)
