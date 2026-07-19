"""bss-clock — process-local scenario clock.

Every BSS service imports ``now()`` from here instead of calling
``datetime.now(timezone.utc)`` directly. By default ``now()`` returns
wall-clock UTC; scenarios can flip the process into a frozen or
offset mode via ``freeze`` / ``advance`` / ``unfreeze``.

The state is deliberately per-process. Each service owns its own
clock; scenarios coordinate freeze/advance across services via the
per-service ``/admin-api/v1/clock/*`` admin endpoints.
"""

from .clock import (
    ClockState,
    advance,
    freeze,
    now,
    parse_duration,
    state,
    unfreeze,
)
from .router import clock_admin_router

__all__ = [
    "ClockState",
    "advance",
    "clock_admin_router",
    "freeze",
    "now",
    "parse_duration",
    "state",
    "unfreeze",
]
