"""Unit tests for use_amqp_span context manager."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _FakeMessage:
    headers: dict[str, str | bytes] | None = None


def test_extract_with_no_headers():
    """A message with no headers still yields cleanly."""
    from bss_telemetry import use_amqp_span

    msg = _FakeMessage(headers=None)
    with use_amqp_span(msg):
        pass


def test_extract_with_traceparent_header():
    """A message with a traceparent string header activates context without error."""
    from bss_telemetry import use_amqp_span

    msg = _FakeMessage(
        headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
    )
    with use_amqp_span(msg):
        pass


def test_extract_with_bytes_header_value():
    """aio-pika may give bytes; we decode before extraction."""
    from bss_telemetry import use_amqp_span

    msg = _FakeMessage(
        headers={
            "traceparent": b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        }
    )
    with use_amqp_span(msg):
        pass


def test_extract_with_unrelated_headers():
    """Non-traceparent headers are passed through extract without error."""
    from bss_telemetry import use_amqp_span

    msg = _FakeMessage(headers={"x-bss-actor": "system", "x-bss-channel": "cli"})
    with use_amqp_span(msg):
        pass
