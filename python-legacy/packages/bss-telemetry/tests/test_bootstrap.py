"""Unit tests for configure_telemetry.

The enabled-mode setup is exercised end-to-end in
tests/integration/v0_2_0/ against a real Jaeger backend. These
unit tests cover the disabled-mode no-op path and idempotency.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset bss_telemetry module state between tests."""
    import bss_telemetry.bootstrap as bs

    bs._INSTALLED = False
    yield
    bs._INSTALLED = False


def test_disabled_returns_none(monkeypatch):
    """BSS_OTEL_ENABLED=false short-circuits to a no-op."""
    monkeypatch.setenv("BSS_OTEL_ENABLED", "false")
    from bss_telemetry import configure_telemetry

    result = configure_telemetry(service_name="test")
    assert result is None


def test_disabled_idempotent(monkeypatch):
    """Calling configure twice in disabled mode is safe and returns None twice."""
    monkeypatch.setenv("BSS_OTEL_ENABLED", "false")
    from bss_telemetry import configure_telemetry

    a = configure_telemetry(service_name="test")
    b = configure_telemetry(service_name="test")
    assert a is None
    assert b is None


def test_tracer_returns_callable():
    """tracer(name) returns something with start_as_current_span."""
    from bss_telemetry import tracer

    t = tracer("test")
    assert hasattr(t, "start_as_current_span")
