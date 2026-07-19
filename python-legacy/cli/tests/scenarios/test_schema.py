"""Scenario YAML schema validation tests."""

from __future__ import annotations

import pytest
import yaml
from bss_cli.scenarios.schema import (
    ActionStep,
    AskStep,
    AssertStep,
    Scenario,
)
from pydantic import ValidationError


def _from_yaml(src: str) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(src))


def test_minimal_scenario_parses() -> None:
    s = _from_yaml(
        """
name: minimal
steps: []
"""
    )
    assert s.name == "minimal"
    assert s.steps == []


def test_action_step_parses() -> None:
    s = _from_yaml(
        """
name: with-action
steps:
  - name: make customer
    action: customer.create
    args: {name: "Ck"}
    capture:
      customer_id: "$.id"
"""
    )
    step = s.steps[0]
    assert isinstance(step, ActionStep)
    assert step.action == "customer.create"
    assert step.args == {"name": "Ck"}
    assert step.capture == {"customer_id": "$.id"}


def test_ask_step_parses_expectations() -> None:
    s = _from_yaml(
        """
name: with-ask
steps:
  - name: diagnose
    ask: "fix the blocked subscription {{ sub_id }}"
    expect_tools_called_include:
      - subscription.get
      - subscription.purchase_vas
    expect_tools_not_called:
      - subscription.terminate
    expect_final_state:
      state: active
"""
    )
    step = s.steps[0]
    assert isinstance(step, AskStep)
    assert step.ask.startswith("fix the blocked")
    assert "subscription.get" in step.expect_tools_called_include
    assert step.expect_final_state == {"state": "active"}


def test_assert_step_parses_alias() -> None:
    s = _from_yaml(
        """
name: with-assert
steps:
  - name: check state
    assert:
      tool: subscription.get
      args: {subscription_id: "SUB-1"}
      expect:
        state: active
"""
    )
    step = s.steps[0]
    assert isinstance(step, AssertStep)
    assert step.assert_.tool == "subscription.get"
    assert step.assert_.expect == {"state": "active"}


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(ValidationError):
        _from_yaml(
            """
name: bad
widgets: 1
steps: []
"""
        )


def test_setup_and_teardown_parse() -> None:
    s = _from_yaml(
        """
name: with-setup
setup:
  reset_operational_data: true
  freeze_clock_at: "2026-04-11T09:00:00+08:00"
  reset_sequences: false
teardown:
  unfreeze_clock: true
steps: []
"""
    )
    assert s.setup.reset_operational_data is True
    assert s.setup.freeze_clock_at.startswith("2026-04-11")
    assert s.teardown.unfreeze_clock is True


def test_assert_step_rejects_capture() -> None:
    with pytest.raises(ValidationError):
        _from_yaml(
            """
name: bad-assert
steps:
  - name: x
    assert:
      tool: subscription.get
      args: {}
      expect: {state: active}
    capture:
      foo: "$.bar"
"""
        )
