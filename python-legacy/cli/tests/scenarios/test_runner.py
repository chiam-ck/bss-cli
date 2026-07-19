"""End-to-end runner tests with the action registry mocked.

We patch ``resolve_action`` (imported at runtime inside ``runner``) so
scenarios execute in-process without hitting downstream services. This
tests the deterministic path: action → capture → interpolation → assert.
"""

from __future__ import annotations

import asyncio
from typing import Any

import yaml
from bss_cli.scenarios.runner import run_scenario
from bss_cli.scenarios.schema import Scenario


def _scenario(src: str) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(src))


def _install_stub_actions(monkeypatch, actions: dict[str, Any]) -> list[str]:
    """Replace ``resolve_action`` in the runner + swallow scenario-only setup."""
    called: list[str] = []

    def _resolve(name: str):
        called.append(name)
        if name in actions:
            return actions[name]
        # Silently no-op setup/teardown fan-outs.
        if name in {
            "admin.reset_operational_data",
            "clock.freeze",
            "clock.unfreeze",
            "clock.advance",
        }:
            async def _noop(**_kwargs):
                return {"noop": name}

            return _noop
        raise KeyError(f"unknown action: {name}")

    monkeypatch.setattr("bss_cli.scenarios.runner.resolve_action", _resolve)
    # Context setter is a no-op for tests (no real bss-clients calls happen).
    monkeypatch.setattr(
        "bss_cli.scenarios.runner.use_scenario_context", lambda **_kw: "test"
    )
    return called


# ─── happy path ────────────────────────────────────────────────────────────


def test_action_capture_and_interpolate(monkeypatch) -> None:
    async def create_customer(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"name": "Ck"}
        return {"id": "CUST-42", "name": "Ck"}

    async def get_customer(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"customer_id": "CUST-42"}
        return {"id": "CUST-42", "state": "pending"}

    _install_stub_actions(
        monkeypatch,
        {"customer.create": create_customer, "customer.get": get_customer},
    )

    scenario = _scenario(
        """
name: happy
steps:
  - name: create
    action: customer.create
    args: {name: "Ck"}
    capture:
      cust: "$.id"
  - name: read back
    assert:
      tool: customer.get
      args: {customer_id: "{{ cust }}"}
      expect:
        state: pending
"""
    )

    result = asyncio.run(run_scenario(scenario))
    assert result.ok, [s.error for s in result.steps if not s.ok]
    assert result.steps[0].captured == {"cust": "CUST-42"}
    assert result.steps[1].ok


def test_action_failure_short_circuits(monkeypatch) -> None:
    async def boom(**_kw) -> dict[str, Any]:
        raise RuntimeError("downstream is unhappy")

    _install_stub_actions(monkeypatch, {"customer.create": boom})

    scenario = _scenario(
        """
name: fails
steps:
  - name: first
    action: customer.create
    args: {name: "Ck"}
  - name: second (skipped)
    action: customer.create
    args: {name: "other"}
"""
    )
    result = asyncio.run(run_scenario(scenario))
    assert not result.ok
    assert len(result.steps) == 1
    assert "downstream is unhappy" in result.steps[0].error


def test_assert_failure_reports_dot_path(monkeypatch) -> None:
    async def get_sub(**_kw) -> dict[str, Any]:
        return {"state": "blocked"}

    _install_stub_actions(monkeypatch, {"subscription.get": get_sub})

    scenario = _scenario(
        """
name: assert-fails
steps:
  - name: expect active
    assert:
      tool: subscription.get
      args: {subscription_id: "SUB-1"}
      expect:
        state: active
"""
    )
    result = asyncio.run(run_scenario(scenario))
    assert not result.ok
    assert "state" in result.steps[0].error


def test_unknown_action_surfaces_error(monkeypatch) -> None:
    _install_stub_actions(monkeypatch, {})

    scenario = _scenario(
        """
name: unknown-action
steps:
  - name: bogus
    action: ghost.tool
    args: {}
"""
    )
    result = asyncio.run(run_scenario(scenario))
    assert not result.ok
    assert "ghost.tool" in result.steps[0].error


def test_ask_step_disabled_mode(monkeypatch) -> None:
    _install_stub_actions(monkeypatch, {})

    scenario = _scenario(
        """
name: llm-blocked
steps:
  - name: chat
    ask: "do the thing"
"""
    )
    result = asyncio.run(run_scenario(scenario, mode="disabled"))
    assert not result.ok
    assert "--no-llm" in result.steps[0].error


def test_setup_reset_triggers_admin_fanout(monkeypatch) -> None:
    called = _install_stub_actions(monkeypatch, {})
    scenario = _scenario(
        """
name: uses-reset
setup:
  reset_operational_data: true
  freeze_clock_at: "2026-04-11T09:00:00+00:00"
teardown:
  unfreeze_clock: true
steps: []
"""
    )
    result = asyncio.run(run_scenario(scenario))
    assert result.ok, (result.setup_error, result.teardown_error)
    assert "admin.reset_operational_data" in called
    assert "clock.freeze" in called
    assert "clock.unfreeze" in called
