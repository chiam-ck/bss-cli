"""W3C traceparent propagation helpers for aio-pika.

aio-pika auto-instrumentation handles ``traceparent`` injection on
publish and extraction on consume. This wrapper is the typed
context manager that consumer code in ``bss-events`` uses to
activate the extracted upstream context for the duration of the
handler so child spans (HTTP, SQL, further MQ) attach correctly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from opentelemetry import context as ot_context
from opentelemetry import propagate
from opentelemetry import trace as ot_trace


def current_trace_id() -> str | None:
    """Return the current OTel trace ID as a 32-char hex string, or None.

    Used by domain-event publishers to stamp ``audit.domain_event.trace_id``
    so post-hoc trace lookups (``bss trace for-order ORD-014``) can join
    business records back to their full distributed trace.
    """
    span = ot_trace.get_current_span()
    if span is None:
        return None
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return f"{ctx.trace_id:032x}"


class _AmqpMessage(Protocol):
    """Minimal protocol matching aio_pika.IncomingMessage.headers."""

    @property
    def headers(self) -> dict[str, str | bytes] | None: ...


@contextmanager
def use_amqp_span(message: _AmqpMessage) -> Iterator[None]:
    """Activate the upstream traceparent context from an AMQP message.

    Used by MQ consumers so downstream tool calls (HTTP, SQL, further
    MQ publishes) attach to the trace started by the producer.
    """
    raw = getattr(message, "headers", None) or {}
    headers: dict[str, str] = {}
    for k, v in raw.items():
        headers[k] = v.decode() if isinstance(v, bytes) else str(v)

    ctx = propagate.extract(headers)
    token = ot_context.attach(ctx)
    try:
        yield
    finally:
        ot_context.detach(token)
