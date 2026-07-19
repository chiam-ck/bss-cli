"""Variable interpolation + capture tests."""

from __future__ import annotations

import pytest
from bss_cli.scenarios.context import ScenarioContext


def test_interpolation_substitutes_strings() -> None:
    ctx = ScenarioContext.new(seed={"name": "Ck", "plan": "PLAN_S"})
    out = ctx.interpolate("hello {{ name }} on {{ plan }}")
    assert out == "hello Ck on PLAN_S"


def test_interpolation_preserves_scalar_type_when_whole() -> None:
    ctx = ScenarioContext.new(seed={"count": 5, "tags": ["a", "b"]})
    assert ctx.interpolate("{{ count }}") == 5
    assert ctx.interpolate("{{ tags }}") == ["a", "b"]


def test_interpolation_in_nested_structures() -> None:
    ctx = ScenarioContext.new(seed={"id": "CUST-1", "email": "x@y.z"})
    payload = {
        "customerId": "{{ id }}",
        "contacts": ["{{ email }}"],
        "nested": {"who": "{{ id }}"},
    }
    assert ctx.interpolate(payload) == {
        "customerId": "CUST-1",
        "contacts": ["x@y.z"],
        "nested": {"who": "CUST-1"},
    }


def test_interpolation_missing_variable_raises() -> None:
    ctx = ScenarioContext.new(seed={})
    with pytest.raises(KeyError):
        ctx.interpolate("hi {{ ghost }}")


def test_run_id_is_auto_injected() -> None:
    ctx = ScenarioContext.new()
    assert "run_id" in ctx.variables
    assert len(ctx.variables["run_id"]) >= 4


def test_captures_populate_context() -> None:
    ctx = ScenarioContext.new()
    result = {"id": "CUST-9", "state": "pending", "items": [{"id": "I-1"}]}
    captured = ctx.apply_captures(
        result,
        {"customer_id": "$.id", "item_id": "$.items[0].id"},
    )
    assert captured == {"customer_id": "CUST-9", "item_id": "I-1"}
    # And they're now referenceable in interpolation
    assert ctx.interpolate("{{ customer_id }}") == "CUST-9"


def test_capture_with_missing_path_raises() -> None:
    ctx = ScenarioContext.new()
    with pytest.raises(KeyError):
        ctx.apply_captures({"id": "X"}, {"sub": "$.subscription_id"})
