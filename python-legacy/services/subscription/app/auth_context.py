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


# v0.18 — token-returning helpers for non-HTTP code paths (renewal worker
# and any future scheduled task). HTTP requests use `set_for_request` and
# discard the Token because Starlette runs each request in its own asyncio
# Task and the ContextVar is per-Task. The renewal worker is a SINGLE
# long-lived Task, so values would leak across iterations unless explicitly
# reset — these helpers expose the reset Token to the caller.


def push(
    *,
    actor: str,
    channel: str,
    tenant: str = "DEFAULT",
    service_identity: str = "default",
):
    """Set context, return the Token the caller MUST pass back to ``pop()``.

    Usage:
        token = auth_context.push(actor="system:renewal_worker", channel="system")
        try:
            await do_dispatch(...)
        finally:
            auth_context.pop(token)
    """
    return _current.set(
        AuthContext(
            actor=actor,
            channel=channel,
            tenant=tenant,
            service_identity=service_identity,
        )
    )


def pop(token) -> None:
    """Reset the ContextVar to the value present before the matching push()."""
    _current.reset(token)
