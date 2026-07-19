"""Self-serve portal middlewares.

v0.8 ships ``PortalSessionMiddleware`` — resolves the session cookie
to (session, identity) on every request and attaches the result to
``request.state``. Route-handler enforcement is via the dependencies
in ``bss_self_serve.security``.
"""

from .session import PORTAL_SESSION_COOKIE, PortalSessionMiddleware

__all__ = ["PORTAL_SESSION_COOKIE", "PortalSessionMiddleware"]
