"""v0.9 — stamp_request_span helper contract.

Locks in the per-request span enrichment that all 9 service
RequestIdMiddlewares call. The helper:

- attaches bss.actor / bss.channel / bss.service.identity to the
  current span when one is recording,
- silently no-ops when no span is recording (OTel disabled, or very
  early in request lifecycle),
- never raises into the caller (telemetry must not gate the request
  path).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bss_telemetry import stamp_request_span


def test_stamps_all_three_attributes_on_recording_span():
    span = MagicMock()
    span.is_recording.return_value = True
    with patch("bss_telemetry.request_span.trace.get_current_span", return_value=span):
        stamp_request_span(
            actor="alice",
            channel="cli",
            service_identity="portal_self_serve",
        )
    set_calls = {
        call.args[0]: call.args[1]
        for call in span.set_attribute.call_args_list
    }
    assert set_calls["bss.actor"] == "alice"
    assert set_calls["bss.channel"] == "cli"
    assert set_calls["bss.service.identity"] == "portal_self_serve"


def test_no_op_when_span_not_recording():
    span = MagicMock()
    span.is_recording.return_value = False
    with patch("bss_telemetry.request_span.trace.get_current_span", return_value=span):
        stamp_request_span(actor="alice", channel="cli", service_identity="default")
    span.set_attribute.assert_not_called()


def test_no_op_when_get_current_span_returns_none():
    with patch(
        "bss_telemetry.request_span.trace.get_current_span", return_value=None
    ):
        # Must not raise even though span is None.
        stamp_request_span(actor="alice", channel="cli", service_identity="default")


def test_no_op_when_get_current_span_raises():
    with patch(
        "bss_telemetry.request_span.trace.get_current_span",
        side_effect=RuntimeError("no provider"),
    ):
        # Must swallow the exception — observability never gates the request.
        stamp_request_span(actor="alice", channel="cli", service_identity="default")


def test_skips_unset_attributes():
    """None / empty string values are skipped — the span doesn't get noise attrs."""
    span = MagicMock()
    span.is_recording.return_value = True
    with patch("bss_telemetry.request_span.trace.get_current_span", return_value=span):
        stamp_request_span(actor=None, channel="", service_identity="portal_self_serve")
    set_calls = {
        call.args[0]: call.args[1]
        for call in span.set_attribute.call_args_list
    }
    assert "bss.actor" not in set_calls
    assert "bss.channel" not in set_calls
    assert set_calls["bss.service.identity"] == "portal_self_serve"
