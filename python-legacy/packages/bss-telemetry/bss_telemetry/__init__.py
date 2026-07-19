"""bss-telemetry — OpenTelemetry bootstrap for BSS-CLI services.

Each service calls ``configure_telemetry(service_name="...")`` once
during lifespan startup. This wires the SDK with OTLP/HTTP export
plus auto-instrumentation for FastAPI, httpx (outbound), asyncpg
(via SQLAlchemy), and aio-pika (MQ publish/consume). W3C
``traceparent`` propagates automatically.

The function is idempotent (safe in tests) and never raises into
the caller — observability must not gate the business path.

Manual span sites should use ``tracer(name)`` from this package and
attach attributes via the constants in ``semconv``. See spec §2a
of phases/V0_2_0.md for the canonical 3 sites.
"""

from . import semconv
from .bootstrap import configure_telemetry, tracer
from .jaeger import JaegerClient, JaegerError
from .propagation import current_trace_id, use_amqp_span
from .request_span import stamp_request_span

__all__ = [
    "JaegerClient",
    "JaegerError",
    "configure_telemetry",
    "current_trace_id",
    "semconv",
    "stamp_request_span",
    "tracer",
    "use_amqp_span",
]
