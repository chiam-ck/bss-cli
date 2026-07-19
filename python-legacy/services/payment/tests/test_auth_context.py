"""v0.9 — AuthContext.service_identity contract.

Locks in the v0.9 dataclass shape and the middleware bridge: a request
that arrives with ``scope["service_identity"]`` populated by
``BSSApiTokenMiddleware`` lands in ``auth_context.current().service_identity``
once ``RequestIdMiddleware`` has run.

Tested at the payment service since the bridge code is identical across
all 9 services (mechanical sweep). Catching a regression here catches it
everywhere.
"""

from __future__ import annotations

import pytest
from app import auth_context


def test_default_auth_context_has_default_identity():
    ctx = auth_context.AuthContext()
    assert ctx.service_identity == "default"


def test_set_for_request_accepts_service_identity():
    auth_context.set_for_request(
        actor="alice",
        tenant="DEFAULT",
        channel="cli",
        service_identity="portal_self_serve",
    )
    ctx = auth_context.current()
    assert ctx.actor == "alice"
    assert ctx.channel == "cli"
    assert ctx.service_identity == "portal_self_serve"


def test_set_for_request_defaults_when_missing():
    """Tests that bypass middleware (direct set_for_request) get 'default'."""
    auth_context.set_for_request(actor="bob", tenant="DEFAULT", channel="cli")
    ctx = auth_context.current()
    assert ctx.service_identity == "default"


@pytest.mark.asyncio
async def test_request_id_middleware_bridges_scope_key_to_auth_context():
    """Pure unit test of the middleware → auth_context bridge.

    Constructs an ASGI scope with ``service_identity`` set (as
    BSSApiTokenMiddleware would after token validation) and runs
    RequestIdMiddleware against it. After the inner app runs, the
    auth_context must reflect the scope value.
    """
    from app.middleware import RequestIdMiddleware

    captured: dict[str, str | None] = {}

    async def inner_app(scope, receive, send):
        captured["service_identity"] = auth_context.current().service_identity
        # Minimal valid HTTP response so the middleware completes its
        # try/except path without raising.
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b""})

    mw = RequestIdMiddleware(inner_app)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [(b"x-bss-actor", b"alice"), (b"x-bss-channel", b"cli")],
        "service_identity": "portal_self_serve",  # set by BSSApiTokenMiddleware
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)

    assert captured["service_identity"] == "portal_self_serve"


@pytest.mark.asyncio
async def test_request_id_middleware_defaults_identity_when_scope_key_absent():
    """If BSSApiTokenMiddleware didn't run (test paths), default to 'default'."""
    from app.middleware import RequestIdMiddleware

    captured: dict[str, str | None] = {}

    async def inner_app(scope, receive, send):
        captured["service_identity"] = auth_context.current().service_identity
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b""})

    mw = RequestIdMiddleware(inner_app)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [],
        # NO service_identity key — simulates bypassed perimeter middleware.
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await mw(scope, receive, send)

    assert captured["service_identity"] == "default"
