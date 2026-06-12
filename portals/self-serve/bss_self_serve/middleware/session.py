"""PortalSessionMiddleware — resolve cookie -> (session, identity).

Pure ASGI middleware (NOT BaseHTTPMiddleware) so SSE responses on
``/agent/events/{session_id}`` keep streaming. BaseHTTPMiddleware
buffers responses and breaks long-lived streams.

Behaviour (V0_8_0.md §2.1):

1. Read ``bss_portal_session`` cookie from incoming request.
2. Resolve via ``bss_portal_auth.current_session(db, cookie_value)``
   in a per-request DB session; updates ``last_seen_at``.
3. Attach ``request.state.session`` (SessionView | None),
   ``request.state.identity`` (IdentityView | None),
   ``request.state.customer_id`` (str | None — may be None for
   verified-but-unlinked identities).
4. Call ``rotate_if_due`` — past TTL/2, mint a new session and
   revoke the old, then write the new id back as a Set-Cookie.
5. On miss / revoked / expired: leave request.state.* as None and
   do not write a Set-Cookie (the route's own logout path is the
   only thing that clears the cookie).

Doctrine: this is the ONLY place that reads the cookie header off the
ASGI scope or sets the session cookie. Route handlers never touch
cookies directly — they go through the auth helpers and let
middleware do the rest.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Final

import structlog
from bss_clients.base import set_context as set_bss_context
from bss_portal_auth import (
    Settings as PortalAuthSettings,
)
from bss_portal_auth import (
    current_session,
    rotate_if_due,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger(__name__)

PORTAL_SESSION_COOKIE: Final[str] = "bss_portal_session"


class PortalSessionMiddleware:
    """Resolve the session cookie + bind state to request.state.

    Constructor takes the DB session_factory directly so the middleware
    can be wired in main.py before lifespan runs the engine setup
    (factory is closed-over at request time).
    """

    def __init__(self, app: ASGIApp, *, session_factory_attr: str = "db_session_factory"):
        self.app = app
        self._session_factory_attr = session_factory_attr

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Empty defaults — every downstream route can rely on these
        # attributes existing without a hasattr() dance.
        state = scope.setdefault("state", {})
        state["session"] = None
        state["identity"] = None
        state["customer_id"] = None

        # v0.11 — stamp the bss-clients ContextVars so every outbound
        # call from this request carries ``X-BSS-Channel: portal-self-serve``.
        # CRM's interaction auto-log reads the channel header off the
        # incoming request and writes it onto every interaction row, so
        # forensic queries answer "which surface initiated this" without
        # ambiguity. Actor defaults to ``portal-anon`` until the cookie
        # resolves to a verified identity below; the resolved identity's
        # email then overrides via a second set_bss_context call. The
        # ContextVar set here is per-asyncio-Task, so concurrent requests
        # don't bleed into each other.
        request_id = ""
        for k, v in scope.get("headers") or []:
            if k == b"x-request-id":
                request_id = v.decode("latin-1", errors="replace")
                break
        set_bss_context(
            actor="portal-anon",
            channel="portal-self-serve",
            request_id=request_id,
        )

        cookie_value = _read_cookie(scope, PORTAL_SESSION_COOKIE)

        rotated_session_id: str | None = None
        if cookie_value:
            try:
                factory = _factory_from_scope(scope, self._session_factory_attr)
            except RuntimeError:
                # Lifespan didn't wire the factory — log once and skip.
                # Fail-open here is correct: portal is still serving public
                # pages and lifespan errors will surface elsewhere.
                log.warning("portal_auth.middleware.no_db_factory")
                await self.app(scope, receive, send)
                return

            async with factory() as db:
                pair = await current_session(db, cookie_value)
                if pair is not None:
                    sess_view, identity_view = pair
                    state["session"] = sess_view
                    state["identity"] = identity_view
                    state["customer_id"] = identity_view.customer_id

                    # v0.11 — once the verified identity resolves, lift
                    # actor from ``portal-anon`` to the identity's email.
                    # Channel stays ``portal-self-serve``; that's the
                    # surface, not the user.
                    set_bss_context(
                        actor=identity_view.email,
                        channel="portal-self-serve",
                        request_id=request_id,
                    )

                    rotated = await rotate_if_due(db, sess_view.id)
                    if rotated is not None:
                        rotated_session_id = rotated.id
                        # Keep request.state coherent with the new id so
                        # the same request sees the rotated session.
                        state["session"] = rotated
                await db.commit()

        await self._send_with_cookie(
            scope, receive, send, set_cookie_value=rotated_session_id
        )

    async def _send_with_cookie(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        set_cookie_value: str | None,
    ) -> None:
        """Wrap ``send`` so we can inject Set-Cookie on the outgoing response."""

        if set_cookie_value is None:
            await self.app(scope, receive, send)
            return

        cookie_header = build_session_cookie(set_cookie_value)

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"set-cookie", cookie_header.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


# ── helpers ──────────────────────────────────────────────────────────────


def _read_cookie(scope: Scope, name: str) -> str | None:
    for k, v in scope.get("headers", []):
        if k.lower() == b"cookie":
            jar = SimpleCookie()
            jar.load(v.decode("latin-1"))
            morsel = jar.get(name)
            if morsel is None:
                return None
            return morsel.value
    return None


def _factory_from_scope(scope: Scope, attr: str):
    """Pull the session factory off the FastAPI app (lifespan attaches it)."""
    app = scope.get("app")
    if app is None:
        raise RuntimeError("ASGI scope missing 'app' — middleware not in FastAPI")
    factory = getattr(getattr(app, "state", None), attr, None)
    if factory is None:
        raise RuntimeError(f"app.state.{attr} not set — wire it in lifespan")
    return factory


def build_session_cookie(session_id: str, *, max_age: int | None = None) -> str:
    """Compose the Set-Cookie header value for the portal session.

    Doctrine (V0_8_0.md §2.1):
    * HttpOnly — JS can't read it (defends XSS exfil).
    * Secure — only sent over HTTPS, unless ``BSS_PORTAL_DEV_INSECURE_COOKIE=1``.
    * SameSite=Lax — defangs CSRF on top-level navigations while keeping
      the magic-link click-through working.
    * Path=/ — the cookie applies portal-wide.
    * Max-Age=86400 (24h) — matches the session TTL ceiling.
    """
    settings = PortalAuthSettings()
    if max_age is None:
        max_age = settings.BSS_PORTAL_SESSION_TTL_S

    parts = [f"{PORTAL_SESSION_COOKIE}={session_id}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if not settings.BSS_PORTAL_DEV_INSECURE_COOKIE:
        parts.append("Secure")
    parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def build_clear_cookie() -> str:
    """Set-Cookie value that clears the portal session client-side.

    Used by the logout route. Same attrs as the live cookie so the
    browser actually overwrites it (browsers are picky about matching
    Path / Domain).
    """
    settings = PortalAuthSettings()
    parts = [
        f"{PORTAL_SESSION_COOKIE}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=0",
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    ]
    if not settings.BSS_PORTAL_DEV_INSECURE_COOKIE:
        parts.insert(2, "Secure")
    return "; ".join(parts)
