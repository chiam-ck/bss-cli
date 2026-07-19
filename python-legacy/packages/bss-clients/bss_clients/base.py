"""BSSClient base — httpx.AsyncClient with timeouts, typed errors, auth hook.

Design rules:
- Timeouts are mandatory and per-method. Default 5s, overridable per call.
- No automatic retries. A 503 is a fact — the caller decides.
- Typed errors: NotFound ≠ ServerError ≠ PolicyViolationFromServer.
- Header propagation: X-BSS-Actor, X-BSS-Channel, X-Request-ID from context.
- AuthProvider called on every outgoing request.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

import httpx

from .auth import AuthProvider, NoAuthProvider
from .errors import ClientError, NotFound, PolicyViolationFromServer, ServerError, Timeout

# Context headers are injected by the calling service's middleware.
# bss-clients reads them from a ContextVar so cross-service calls propagate
# the original actor/channel/request-id.
_actor_var: ContextVar[str] = ContextVar("bss_actor", default="system")
_channel_var: ContextVar[str] = ContextVar("bss_channel", default="system")
_request_id_var: ContextVar[str] = ContextVar("bss_request_id", default="")

# v0.9 — per-call X-BSS-API-Token override. When set in the current
# asyncio Context, bss-clients overrides whatever token the
# AuthProvider produced. Used by ``astream_once(service_identity=...)``
# so an orchestrator-built client can carry a different identity for
# tool calls initiated through a specific portal surface — without
# rebuilding the client bundle. Empty string means "no override; let
# the AuthProvider win" (the v0.3 + v0.6 behaviour).
_service_identity_token_var: ContextVar[str] = ContextVar(
    "bss_service_identity_token",
    default="",
)


def set_context(*, actor: str, channel: str, request_id: str) -> None:
    """Called by service middleware to propagate context to outgoing calls."""
    _actor_var.set(actor)
    _channel_var.set(channel)
    _request_id_var.set(request_id)


def set_service_identity_token(token: str | None):  # noqa: ANN201 — Token type lazy
    """Override the outbound X-BSS-API-Token for the current Context.

    Returns a ``contextvars.Token`` so callers can reset to the prior
    value on exit::

        reset_token = set_service_identity_token(portal_token)
        try:
            # downstream bss-clients calls carry portal_token
            await do_work()
        finally:
            reset_service_identity_token(reset_token)

    Pass ``None`` or empty to clear the override (the AuthProvider's
    token wins again). The override is per-Context, so concurrent
    asyncio tasks each see their own isolated value.
    """
    return _service_identity_token_var.set(token or "")


def reset_service_identity_token(reset_token) -> None:  # noqa: ANN001 — Token type lazy
    """Reset the override to its prior value. Counterpart to set_service_identity_token."""
    _service_identity_token_var.reset(reset_token)


class BSSClient:
    """Base HTTP client for service-to-service calls."""

    def __init__(
        self,
        base_url: str,
        auth_provider: AuthProvider | None = None,
        timeout: float = 5.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._auth = auth_provider or NoAuthProvider()
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})

        # Auth headers from provider
        auth_headers = await self._auth.get_headers()
        headers.update(auth_headers)

        # v0.9 — per-Context X-BSS-API-Token override. When set (e.g. by
        # astream_once(service_identity=...)), this wins over the
        # provider's token so the downstream call carries the right
        # identity for the surface that initiated the action. Empty
        # string means "no override".
        identity_token_override = _service_identity_token_var.get()
        if identity_token_override:
            headers["X-BSS-API-Token"] = identity_token_override

        # Context headers — propagate actor/channel/request-id across hops
        headers.setdefault("X-BSS-Actor", _actor_var.get())
        headers.setdefault("X-BSS-Channel", _channel_var.get())
        request_id = _request_id_var.get() or str(uuid.uuid4())
        headers.setdefault("X-Request-ID", request_id)

        kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout

        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise Timeout(f"{method} {path} timed out")

        return await self._handle_response(resp)

    async def _handle_response(self, resp: httpx.Response) -> httpx.Response:
        if resp.status_code == 404:
            raise NotFound(resp.text)

        if resp.status_code == 422:
            try:
                body = resp.json()
            except Exception:
                raise ClientError(422, resp.text)
            if body.get("code") == "POLICY_VIOLATION":
                raise PolicyViolationFromServer(
                    rule=body["reason"],
                    message=body["message"],
                    context=body.get("context", {}),
                )
            raise ClientError(422, resp.text)

        if resp.status_code >= 500:
            raise ServerError(resp.status_code, resp.text)

        resp.raise_for_status()
        return resp

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> BSSClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
