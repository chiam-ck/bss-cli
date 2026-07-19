"""bss-events — shared audit-events query router.

Every BSS service mounts ``audit_events_router()`` under
``/audit-api/v1`` to expose ``GET /events`` — a filterable read-only
view onto ``audit.domain_event``. Scenarios use this (via the
``AuditClient`` in ``bss-clients``) to assert on what happened during
a scenario run without reaching into another service's database.

The endpoint is intentionally *unguarded*. ``audit.domain_event`` is
derivable from a normal read path; nothing mutates. Phase 12 will add
RBAC scoping — for v0.1 any caller may read the full window.
"""

from .consumer import bind_consumer, declare_retry_exchange
from .relay import Relay, start_relay
from .router import audit_events_router

__all__ = [
    "audit_events_router",
    "bind_consumer",
    "declare_retry_exchange",
    "Relay",
    "start_relay",
]
