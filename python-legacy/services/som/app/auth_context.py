"""Auth context — hardcoded for v0.1, replaced in Phase 12.

v0.9 added ``service_identity`` — the resolved name of the named-token
that authenticated the inbound request (``"default"`` for orchestrator
/ CSR / scenarios, ``"portal_self_serve"`` for the customer portal,
``"partner_<name>"`` for future partner integrations). Populated by
RequestIdMiddleware from the scope key ``BSSApiTokenMiddleware`` set;
never read from a separate header.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class AuthContext:
    actor: str = "system"
    tenant: str = "DEFAULT"
    roles: list[str] = field(default_factory=lambda: ["admin"])
    permissions: list[str] = field(default_factory=lambda: ["*"])
    channel: str = "system"
    service_identity: str = "default"


_current: ContextVar[AuthContext] = ContextVar("auth_context", default=AuthContext())


def current() -> AuthContext:
    return _current.get()


def set_for_request(
    *,
    actor: str,
    channel: str,
    tenant: str = "DEFAULT",
    service_identity: str = "default",
) -> None:
    _current.set(
        AuthContext(
            actor=actor,
            channel=channel,
            tenant=tenant,
            service_identity=service_identity,
        )
    )


def has_permission(permission: str) -> bool:
    ctx = current()
    return "*" in ctx.permissions or permission in ctx.permissions
