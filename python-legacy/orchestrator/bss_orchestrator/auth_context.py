"""Orchestrator-side auth context — the customer scoping seam (v0.12).

The ``customer_self_serve`` profile's ``*.mine`` tool wrappers read the
logged-in customer's id from ``auth_context.current().actor``. This
module is that seam.

It mirrors the service-side ``auth_context.py`` shape (``current()`` +
``AuthContext`` dataclass) so the wrappers read uniformly regardless
of where they execute. The orchestrator's value is set by
``astream_once(actor=...)`` for the duration of one stream and reset
on exit; concurrent streams in different asyncio Contexts each see
their own actor.

Doctrine: the wrappers MUST use ``current()`` rather than reaching
into ``bss_clients._actor_var``. The two values are coupled (the chat
route passes the same id to both) but the orchestrator-side context
is the explicit, documented seam — the bss-clients ContextVar is an
internal implementation detail of the HTTP propagation layer.

Per CLAUDE.md anti-pattern (v0.12+): no ``*.mine`` tool may accept a
``customer_id`` parameter — the binding always comes from here.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthContext:
    """The orchestrator-process view of who is asking.

    Currently two fields; mirrors the service-side richer
    AuthContext (with tenant, roles, etc.) as the surface that needs
    to grow when Phase 12 ships RBAC. For v0.12 only ``actor`` and
    ``transcript`` are consumed.

    actor is the customer id (``CUST-NNN``) for chat sessions, or
    ``None`` outside a chat-scoped invocation.

    transcript is the running text of the current chat session up to
    the most recent turn. ``case.open_for_me`` reads this when a
    customer escalates so the resulting case carries the conversation
    that prompted it. The chat route populates it when starting an
    ``astream_once`` invocation.
    """

    actor: str | None = None
    transcript: str = ""


_NONE = AuthContext(actor=None)
_current: ContextVar[AuthContext] = ContextVar("bss_orch_auth_context", default=_NONE)


def current() -> AuthContext:
    """Return the auth context for the current asyncio Context.

    Outside a chat-scoped ``astream_once`` invocation this returns the
    default empty AuthContext (``actor=None``); ``*.mine`` wrappers
    treat that as a programming error and raise.
    """
    return _current.get()


def set_actor(actor: str, *, transcript: str = "") -> Token[AuthContext]:
    """Bind ``actor`` (and optionally a chat transcript) for the rest
    of the current Context.

    Returns a Token so callers can ``reset_actor`` on exit. Use as::

        token = set_actor(customer_id, transcript=running_text)
        try:
            ...
        finally:
            reset_actor(token)
    """
    return _current.set(AuthContext(actor=actor, transcript=transcript))


def reset_actor(token: Token[AuthContext]) -> None:
    """Reset to the AuthContext that was current before ``set_actor``."""
    _current.reset(token)
