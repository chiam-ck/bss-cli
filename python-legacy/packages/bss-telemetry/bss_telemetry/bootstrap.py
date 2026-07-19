"""Configure OpenTelemetry once per service process.

Call ``configure_telemetry(service_name="crm")`` from each service's
lifespan. Behavior:

- Reads ``BSS_OTEL_*`` env vars via pydantic-settings
- If ``BSS_OTEL_ENABLED=false``, returns ``None`` (no-op for tests)
- Builds a ``TracerProvider`` with ``OTLPSpanExporter`` (HTTP/protobuf)
  and a ``BatchSpanProcessor``
- Installs auto-instrumentors: FastAPI, HTTPX (outbound), AsyncPG
  (via SQLAlchemy), AioPika (MQ publish/consume)
- Idempotent — subsequent calls in the same process return the same
  provider without re-instrumenting
- Never raises into the caller — observability must not gate startup
"""

from __future__ import annotations

import atexit
from typing import TYPE_CHECKING

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer

from .config import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

# Heavy instrumentation imports happen lazily inside configure_telemetry().
# This keeps `import bss_telemetry` light enough that the semconv and
# propagation modules can be exercised in unit tests without pulling in
# fastapi / httpx / asyncpg / aio_pika.

log = structlog.get_logger(__name__)

_INSTALLED = False

# URLs the FastAPI instrumentor must NOT create server spans for.
# Docker healthcheck hits /health every 10s on every service — left
# unfiltered, the Jaeger UI is 99% noise. Comma-separated regex per
# OTel spec: https://opentelemetry-python-contrib.readthedocs.io/.../fastapi/
_EXCLUDED_URLS = "/health,/health/.*,/metrics"


def configure_telemetry(  # noqa: ANN201 — return type lazy-imported
    service_name: str,
    *,
    app: "FastAPI | None" = None,
):
    """Bootstrap OTel tracing for one service process.

    Pass ``app`` (the running FastAPI instance) so the instrumentor
    wraps the live app. ``FastAPIInstrumentor().instrument()`` only
    patches the FastAPI class for *future* instances — services that
    create their app at import time and call configure_telemetry from
    lifespan would otherwise have an unwrapped server, leaving HTTP
    request handlers without a span and ``current_trace_id()``
    returning None.

    Returns the TracerProvider on success, ``None`` if telemetry is
    disabled or setup failed. Idempotent within a process.
    """
    global _INSTALLED
    # Heavy SDK imports — only loaded if/when this function actually runs.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.aio_pika import AioPikaInstrumentor
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    if _INSTALLED:
        provider = trace.get_tracer_provider()
        if app is not None and not getattr(app, "_bss_otel_instrumented", False):
            FastAPIInstrumentor.instrument_app(app, excluded_urls=_EXCLUDED_URLS)
            app.middleware_stack = None
            app._bss_otel_instrumented = True  # type: ignore[attr-defined]
        return provider if isinstance(provider, TracerProvider) else None

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 — observability never crashes startup
        log.warning("telemetry.config_failed", error=str(exc), service=service_name)
        _INSTALLED = True
        return None

    if not settings.BSS_OTEL_ENABLED:
        log.info("telemetry.disabled", service=service_name)
        _INSTALLED = True
        return None

    try:
        resource = Resource.create(
            {
                "service.name": f"{settings.BSS_OTEL_SERVICE_NAME_PREFIX}-{service_name}",
                "service.version": settings.BSS_OTEL_SERVICE_VERSION,
            }
        )
        provider = TracerProvider(
            resource=resource,
            sampler=TraceIdRatioBased(settings.BSS_OTEL_SAMPLING_RATIO),
        )
        exporter = OTLPSpanExporter(
            endpoint=f"{settings.BSS_OTEL_EXPORTER_OTLP_ENDPOINT.rstrip('/')}/v1/traces",
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Wrap the live FastAPI instance if provided. Required because
        # services create the app at import time, before this function
        # runs, and the global instrument() only catches *future* instances.
        # We must also invalidate the middleware stack cache — Starlette
        # builds and caches it on the FIRST __call__, which is the lifespan
        # invocation; by the time configure_telemetry runs in lifespan
        # startup the stack is already built without OTel. Forcing the
        # cache to None makes the next real request rebuild the stack
        # with the OTel server-span middleware in place.
        if app is not None:
            FastAPIInstrumentor.instrument_app(app, excluded_urls=_EXCLUDED_URLS)
            app.middleware_stack = None
            app._bss_otel_instrumented = True  # type: ignore[attr-defined]
        else:
            FastAPIInstrumentor().instrument(excluded_urls=_EXCLUDED_URLS)
        HTTPXClientInstrumentor().instrument()
        AsyncPGInstrumentor().instrument()
        AioPikaInstrumentor().instrument()

        # Short-lived processes (CLI, scripts) won't see the
        # BatchSpanProcessor's interval (~5s) before exit. Register an
        # atexit hook to force-flush. Long-running processes (services)
        # also benefit on graceful shutdown.
        atexit.register(_safe_force_flush, provider)

        # Service lifespans want this in their log stream; CLI does
        # NOT — short-lived process, the user shouldn't see ops noise
        # on every invocation, and structlog default is stdout which
        # would corrupt --json pipes.
        if service_name != "cli":
            log.info(
                "telemetry.configured",
                service=service_name,
                endpoint=settings.BSS_OTEL_EXPORTER_OTLP_ENDPOINT,
                sampling_ratio=settings.BSS_OTEL_SAMPLING_RATIO,
                fastapi_app_wrapped=app is not None,
            )
        _INSTALLED = True
        return provider
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry.setup_failed", error=str(exc), service=service_name)
        _INSTALLED = True
        return None


def _safe_force_flush(provider) -> None:
    """Atexit hook — flush queued spans without raising."""
    try:
        provider.force_flush(timeout_millis=2000)
    except Exception:  # noqa: BLE001
        pass


def tracer(name: str) -> Tracer:
    """Get a Tracer for manual span creation.

    Reserved for the 3 documented manual-span sites (V0_2_0.md §2a).
    All other instrumentation is automatic.
    """
    return trace.get_tracer(name)
