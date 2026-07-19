"""Per-request OTel span enrichment helpers (v0.9+).

Each service's RequestIdMiddleware calls ``stamp_request_span`` after
resolving caller context (actor / channel / service_identity) so the
auto-instrumented FastAPI server span carries those attributes. The
``bss trace`` swimlane renderer surfaces them as a per-span column.

The helper is intentionally tiny and import-light: business logic
stays out of services/ and policies/ per doctrine, but the
framework-edge middleware files are explicitly allowed to import OTel
(the doctrine grep guard targets ``services/*/app/services/`` and
``services/*/app/policies/``, not ``middleware.py``).
"""

from __future__ import annotations

from opentelemetry import trace

from . import semconv


def stamp_request_span(
    *,
    actor: str | None = None,
    channel: str | None = None,
    service_identity: str | None = None,
) -> None:
    """Attach BSS caller-context attributes to the current OTel span.

    No-op if no span is recording (OTel disabled, or the FastAPI
    instrumentor hasn't created the server span yet — possible on
    very-early-failure paths). Never raises: observability must not
    gate the business path.
    """
    try:
        span = trace.get_current_span()
        if not span or not span.is_recording():
            return
        if actor:
            span.set_attribute(semconv.BSS_ACTOR, actor)
        if channel:
            span.set_attribute(semconv.BSS_CHANNEL, channel)
        if service_identity:
            span.set_attribute(semconv.BSS_SERVICE_IDENTITY, service_identity)
    except Exception:  # noqa: BLE001
        # Telemetry never breaks request handling.
        pass
