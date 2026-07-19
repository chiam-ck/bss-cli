"""Scenario execution — parse → setup → steps → teardown → report.

Invocation:

    result = await run_scenario(scenario, mode="auto")

``mode`` gates ``ask:`` steps:

* ``"auto"``    — action/assert deterministic, ask: → LLM executor
* ``"disabled"`` — ask: steps fail with :class:`LLMDisabled`
* ``"forced"``  — experimental; Task #6 wires conversion of action steps
  into natural-language equivalents. Task-#4 scope treats ``forced`` like
  ``auto`` (no rewrite happens).

The runner is a straight list-walk: no conditionals, no retries. Each
step is timed; failures short-circuit the remainder of ``steps`` but
teardown always runs. Exceptions inside teardown are captured but don't
overwrite the primary failure.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from bss_clients.errors import ClientError, PolicyViolationFromServer
from bss_orchestrator.context import use_scenario_context
from pydantic import ValidationError

from .actions import resolve_action
from .assertions import AssertionResult, evaluate_expect, poll_until
from .context import ScenarioContext
from .file_step import run_file_step
from .http_step import run_http_step
from .llm_executor import LLMDisabled, execute_ask_step
from .schema import (
    ActionStep,
    AskStep,
    AssertStep,
    FileReadStep,
    HTTPStep,
    LLMMode,
    Scenario,
    Step,
    Teardown,
)

# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    name: str
    kind: str  # action | ask | assert
    ok: bool
    duration_ms: float
    detail: str = ""
    captured: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    assertion: AssertionResult | None = None
    error: str | None = None


@dataclass
class ScenarioResult:
    scenario: str
    ok: bool
    duration_ms: float
    steps: list[StepResult] = field(default_factory=list)
    setup_error: str | None = None
    teardown_error: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────────


def load_scenario(path: str | Path) -> Scenario:
    """Parse + validate a YAML file. Raises :class:`pydantic.ValidationError`."""
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return Scenario.model_validate(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────────────


async def run_scenario(
    scenario: Scenario,
    *,
    mode: LLMMode = "auto",
) -> ScenarioResult:
    """Execute ``scenario`` end-to-end. Never raises — packages failures into result."""
    t0 = time.monotonic()
    use_scenario_context(name=scenario.name)

    context = ScenarioContext.new(
        seed={**scenario.setup.variables, **scenario.variables}
    )
    result = ScenarioResult(scenario=scenario.name, ok=True, duration_ms=0.0)

    # Setup
    try:
        await _run_setup(scenario, context)
    except Exception as e:
        result.ok = False
        result.setup_error = _format_error(e)
        result.duration_ms = (time.monotonic() - t0) * 1000
        result.variables = context.snapshot()
        return result

    # Steps — short-circuit on first failure but still run teardown.
    for step in scenario.steps:
        step_result = await _run_step(step, context, mode=mode)
        result.steps.append(step_result)
        if not step_result.ok:
            result.ok = False
            break

    # Teardown — always runs, errors don't mask step failures.
    try:
        await _run_teardown(scenario.teardown)
    except Exception as e:
        result.teardown_error = _format_error(e)
        # Teardown failure → scenario fails even if steps all passed.
        result.ok = False

    result.duration_ms = (time.monotonic() - t0) * 1000
    result.variables = context.snapshot()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Setup + teardown
# ─────────────────────────────────────────────────────────────────────────────


async def _run_setup(scenario: Scenario, ctx: ScenarioContext) -> None:
    setup = scenario.setup
    if setup.reset_operational_data:
        action = resolve_action("admin.reset_operational_data")
        await action(reset_sequences=setup.reset_sequences)
    if setup.freeze_clock_at is not None:
        action = resolve_action("clock.freeze")
        at = ctx.interpolate(setup.freeze_clock_at)
        await action(at=at)


async def _run_teardown(teardown: Teardown) -> None:
    if teardown.unfreeze_clock:
        action = resolve_action("clock.unfreeze")
        await action()


# ─────────────────────────────────────────────────────────────────────────────
# Step dispatch
# ─────────────────────────────────────────────────────────────────────────────


async def _run_step(
    step: Step, ctx: ScenarioContext, *, mode: LLMMode
) -> StepResult:
    if isinstance(step, ActionStep):
        return await _run_action(step, ctx)
    if isinstance(step, AssertStep):
        return await _run_assert(step, ctx)
    if isinstance(step, AskStep):
        return await _run_ask(step, ctx, mode=mode)
    if isinstance(step, HTTPStep):
        return await run_http_step(step, ctx, format_error=_format_error)
    if isinstance(step, FileReadStep):
        return await run_file_step(step, ctx, format_error=_format_error)
    # pydantic guarantees the union — defensive default
    return StepResult(
        name=getattr(step, "name", "?"),
        kind="unknown",
        ok=False,
        duration_ms=0.0,
        error=f"unknown step type: {type(step).__name__}",
    )


async def _run_action(step: ActionStep, ctx: ScenarioContext) -> StepResult:
    t0 = time.monotonic()
    try:
        fn = resolve_action(step.action)
    except KeyError as e:
        return StepResult(
            name=step.name,
            kind="action",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            error=str(e),
        )

    args = ctx.interpolate(step.args)
    try:
        result = await fn(**args)
    except Exception as e:
        return StepResult(
            name=step.name,
            kind="action",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            error=_format_error(e),
        )

    try:
        captured = ctx.apply_captures(result, step.capture)
    except KeyError as e:
        return StepResult(
            name=step.name,
            kind="action",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            result=result,
            error=str(e),
        )

    return StepResult(
        name=step.name,
        kind="action",
        ok=True,
        duration_ms=(time.monotonic() - t0) * 1000,
        captured=captured,
        result=result,
    )


async def _run_assert(step: AssertStep, ctx: ScenarioContext) -> StepResult:
    t0 = time.monotonic()
    call = step.assert_
    try:
        fn = resolve_action(call.tool)
    except KeyError as e:
        return StepResult(
            name=step.name,
            kind="assert",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            error=str(e),
        )

    args = ctx.interpolate(call.args)
    expect = ctx.interpolate(call.expect)

    def fetch() -> Awaitable[Any]:
        return fn(**args)

    try:
        assertion = await poll_until(fetch, expect, call.poll)
    except Exception as e:
        return StepResult(
            name=step.name,
            kind="assert",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            error=_format_error(e),
        )

    return StepResult(
        name=step.name,
        kind="assert",
        ok=assertion.ok,
        duration_ms=(time.monotonic() - t0) * 1000,
        assertion=assertion,
        result=assertion.last_value,
        error=None if assertion.ok else assertion.format(),
    )


async def _run_ask(
    step: AskStep, ctx: ScenarioContext, *, mode: LLMMode
) -> StepResult:
    t0 = time.monotonic()
    allow_llm = mode != "disabled"
    try:
        outcome = await execute_ask_step(step, ctx, allow_llm=allow_llm)
    except LLMDisabled as e:
        return StepResult(
            name=step.name,
            kind="ask",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            error=str(e),
        )
    except Exception as e:
        return StepResult(
            name=step.name,
            kind="ask",
            ok=False,
            duration_ms=(time.monotonic() - t0) * 1000,
            error=_format_error(e),
        )

    # Evaluate LLM-specific expectations — executed once Task #6 wires a real
    # outcome. Kept here so the contract is stable.
    failures: list[str] = []
    called = set(outcome.tools_called)
    missing = [t for t in step.expect_tools_called_include if t not in called]
    forbidden = [t for t in step.expect_tools_not_called if t in called]
    if missing:
        failures.append(f"expected tools not called: {missing}")
    if forbidden:
        failures.append(f"forbidden tools called: {forbidden}")
    if step.expect_final_state:
        fs = evaluate_expect(step.expect_final_state, outcome.final_state_probe)
        if not fs.ok:
            failures.append(f"final state mismatch: {fs.format()}")

    return StepResult(
        name=step.name,
        kind="ask",
        ok=not failures,
        duration_ms=(time.monotonic() - t0) * 1000,
        result=outcome,
        error="\n".join(failures) if failures else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Error formatting
# ─────────────────────────────────────────────────────────────────────────────


def _format_error(e: BaseException) -> str:
    if isinstance(e, PolicyViolationFromServer):
        return f"policy violation: {e.rule}: {e.detail}"
    if isinstance(e, ClientError):
        return f"client error {e.status_code}: {e.detail}"
    if isinstance(e, (ValidationError, ValueError, KeyError)):
        return f"{type(e).__name__}: {e}"
    return "".join(
        traceback.format_exception_only(type(e), e)
    ).strip() or type(e).__name__
