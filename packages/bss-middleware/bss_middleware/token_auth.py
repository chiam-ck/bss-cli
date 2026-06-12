"""Pure ASGI middleware: require X-BSS-API-Token on every BSS request.

Behavior (per V0_3_0.md §1a, extended by V0_9_0.md §1):

1. Path in EXEMPT_PATHS → pass through (no auth).
2. ``X-BSS-API-Token`` header missing → 401 ``AUTH_MISSING_TOKEN``.
3. Header present but no entry in the loaded TokenMap → 401
   ``AUTH_INVALID_TOKEN``. Comparison is constant-time
   (``hmac.compare_digest`` per entry).
4. Header matches an entry → set ``scope["service_identity"]`` to the
   resolved identity and pass through.

The middleware is registered after RequestIdMiddleware in each service's
main.py so that auth runs first (Starlette ``add_middleware`` prepends:
last-added = outermost). The OTel server span (set up by
FastAPIInstrumentor) wraps everything including auth — 401s show up
in Jaeger as 401 spans, which is the correct behavior for ops triage.

v0.9: ``service_identity`` is authoritative. It comes only from the
validated token map, never from a separate header. RequestIdMiddleware
reads ``scope["service_identity"]`` and stamps it onto auth_context.

Log policy: per ``(remote_addr, path)`` pair, the first 401 inside a
60-second window is logged at INFO; subsequent 401s in the same
window are silent. Avoids log spam from probes or misconfigured
clients without losing visibility into a fresh failure mode.
"""

from __future__ import annotations

import json
import time
from typing import Final

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_token import TokenMap, load_token_map_from_env

log = structlog.get_logger(__name__)

EXEMPT_PATHS: Final[frozenset[str]] = frozenset({
    "/health",
    "/health/ready",
    "/health/live",
})

# Per-prefix exemptions. Webhook receivers (v0.14+) authenticate via
# provider signature (svix/stripe/didit_hmac) verified inside the
# route handler, NOT via X-BSS-API-Token. The signing secret stays
# with the receiving service; the BSS perimeter token would prevent
# Resend/Stripe/Didit from ever reaching us.
WEBHOOK_EXEMPT_PATHS: Final[tuple[str, ...]] = ("/webhooks/",)

AUTH_MISSING_TOKEN: Final[str] = "AUTH_MISSING_TOKEN"
AUTH_INVALID_TOKEN: Final[str] = "AUTH_INVALID_TOKEN"

# v0.9 — ASGI scope key for the resolved identity. RequestIdMiddleware
# reads this and stamps it onto auth_context. Treat as a stable contract
# between middlewares; never read by route handlers directly.
SCOPE_SERVICE_IDENTITY: Final[str] = "service_identity"

_LOG_THROTTLE_SECONDS = 60.0


class BSSApiTokenMiddleware:
    """Validate ``X-BSS-API-Token`` against a loaded ``TokenMap``.

    Construction modes (in priority order):

    1. ``token_map=<TokenMap>`` (v0.9+ preferred) — use the supplied
       map verbatim. Lifespan startup typically calls
       ``validate_token_map_present()`` and passes the result here.
    2. ``token=<str>`` (v0.3 backwards-compat) — build a single-entry
       map with identity ``"default"``. Existing tests that pass a
       literal token continue to work.
    3. No args — load the map from env at construction time. The 9
       services' ``app.add_middleware(BSSApiTokenMiddleware)`` (no
       kwargs) keeps working unchanged: each service's lifespan
       already validates env via ``validate_api_token_present`` /
       ``validate_token_map_present`` before the middleware
       constructs.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token_map: TokenMap | None = None,
        token: str | None = None,
    ) -> None:
        self.app = app
        if token_map is not None:
            self._token_map = token_map
        elif token is not None:
            # v0.3 compat — single literal token, identity "default".
            from .api_token import _hash_token  # local import to keep cycle-free

            self._token_map = TokenMap(entries=((_hash_token(token), "default"),))
        else:
            self._token_map = load_token_map_from_env()
        # In-process throttle dict: (remote_addr, path) -> last log timestamp.
        self._last_logged: dict[tuple[str, str], float] = {}

    @property
    def token_map(self) -> TokenMap:
        """The loaded TokenMap. Exposed for diagnostics; do not mutate."""
        return self._token_map

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in EXEMPT_PATHS or _is_webhook_path(path):
            await self.app(scope, receive, send)
            return

        provided = _extract_header(scope, b"x-bss-api-token")
        if not provided:
            self._log_throttled(scope, path, reason="missing")
            await _send_401_json(send, AUTH_MISSING_TOKEN, "X-BSS-API-Token header required")
            return

        identity = self._token_map.lookup(provided)
        if identity is None:
            self._log_throttled(scope, path, reason="wrong")
            await _send_401_json(send, AUTH_INVALID_TOKEN, "invalid API token")
            return

        # v0.9 — authoritative service_identity: only from successful
        # token validation, never from a separate header. Downstream
        # middleware (RequestIdMiddleware) reads this off the scope.
        scope[SCOPE_SERVICE_IDENTITY] = identity

        await self.app(scope, receive, send)

    def _log_throttled(self, scope: Scope, path: str, *, reason: str) -> None:
        client = scope.get("client") or ("unknown", 0)
        remote = client[0] if isinstance(client, (tuple, list)) else "unknown"
        key = (remote, path)
        now = time.monotonic()
        last = self._last_logged.get(key)
        if last is not None and (now - last) < _LOG_THROTTLE_SECONDS:
            return
        self._last_logged[key] = now
        log.info("auth.401", reason=reason, remote=remote, path=path)


def _is_webhook_path(path: str) -> bool:
    """``/webhooks/<provider>`` paths are exempt from BSS token auth.

    Provider signature is verified inside the route handler. See
    ``bss_webhooks.signatures.verify_signature``.
    """
    return any(path.startswith(prefix) for prefix in WEBHOOK_EXEMPT_PATHS)


def _extract_header(scope: Scope, name: bytes) -> str | None:
    """Read a single header value from the ASGI scope. Case-insensitive name match."""
    target = name.lower()
    for k, v in scope.get("headers", []):
        if k.lower() == target:
            return v.decode("latin-1")
    return None


async def _send_401_json(send: Send, code: str, message: str) -> None:
    body = json.dumps({"code": code, "message": message}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})
