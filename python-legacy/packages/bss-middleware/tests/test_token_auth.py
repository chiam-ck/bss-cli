"""Unit tests for BSSApiTokenMiddleware against a throwaway FastAPI app.

Per V0_3_0.md test strategy: prove middleware behavior in isolation
before wiring into real services. Cases:

- /health passes through without a token (exemption)
- Missing X-BSS-API-Token header → 401 AUTH_MISSING_TOKEN
- Wrong token → 401 AUTH_INVALID_TOKEN
- Right token → 200 (handler ran)

Bonus: exemption boundary checks — /healthz must NOT be exempt
(only the literal three paths in EXEMPT_PATHS).
"""

from __future__ import annotations

import json

import pytest
from bss_middleware import (
    AUTH_INVALID_TOKEN,
    AUTH_MISSING_TOKEN,
    SCOPE_SERVICE_IDENTITY,
    TEST_TOKEN,
    BSSApiTokenMiddleware,
    TokenMap,
    load_token_map_from_env,
)
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient


def _make_app(token: str = TEST_TOKEN) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BSSApiTokenMiddleware, token=token)

    @app.get("/")
    async def root():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        return {"ready": True}

    @app.get("/healthz")
    async def healthz():
        # Note: NOT in EXEMPT_PATHS — must require token.
        return {"ok": True}

    @app.post("/webhooks/resend")
    async def webhook_resend():
        # v0.14: webhooks/* exempt from BSS token; provider-signed.
        return {"received": True}

    @app.post("/webhooks/stripe")
    async def webhook_stripe():
        return {"received": True}

    return app


@pytest.fixture
async def client():
    transport = ASGITransport(app=_make_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_exempt_no_token(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200


async def test_health_ready_exempt_no_token(client: AsyncClient) -> None:
    r = await client.get("/health/ready")
    assert r.status_code == 200


async def test_root_without_token_returns_401_missing(client: AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == AUTH_MISSING_TOKEN
    assert "X-BSS-API-Token" in body["message"]


async def test_root_with_wrong_token_returns_401_invalid(client: AsyncClient) -> None:
    r = await client.get("/", headers={"X-BSS-API-Token": "wrong-token-value"})
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == AUTH_INVALID_TOKEN
    # The error message must NOT echo the provided wrong token back.
    assert "wrong-token-value" not in json.dumps(body)


async def test_root_with_correct_token_returns_200(client: AsyncClient) -> None:
    r = await client.get("/", headers={"X-BSS-API-Token": TEST_TOKEN})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_healthz_not_exempt(client: AsyncClient) -> None:
    """Only the literal three paths in EXEMPT_PATHS bypass auth."""
    r = await client.get("/healthz")
    assert r.status_code == 401


async def test_webhooks_resend_exempt_no_token(client: AsyncClient) -> None:
    """v0.14: /webhooks/* exempt from BSS token (provider-signed instead)."""
    r = await client.post("/webhooks/resend")
    assert r.status_code == 200


async def test_webhooks_stripe_exempt_no_token(client: AsyncClient) -> None:
    r = await client.post("/webhooks/stripe")
    assert r.status_code == 200


async def test_webhooks_path_must_have_provider_segment(client: AsyncClient) -> None:
    """``/webhooks`` (no trailing slash) is NOT exempt — must be /webhooks/<provider>."""
    r = await client.post("/webhooks")
    assert r.status_code == 401


async def test_header_name_is_case_insensitive(client: AsyncClient) -> None:
    """ASGI headers are bytes; check case-insensitive lookup."""
    r = await client.get("/", headers={"x-bss-api-token": TEST_TOKEN})
    assert r.status_code == 200


async def test_token_comparison_uses_constant_time() -> None:
    """v0.9: comparison happens inside TokenMap.lookup via hmac.compare_digest.

    The middleware delegates the lookup; the per-entry compare is in
    ``api_token.TokenMap.lookup``. Assert the middleware does NOT do
    a naive ``==`` against any stored token.
    """
    import inspect

    from bss_middleware import token_auth

    src = inspect.getsource(token_auth)
    # Negative: no naive comparison of `provided` against any stored token.
    assert "provided == " not in src
    assert "provided.encode() ==" not in src
    # The middleware must route through the map's lookup helper, not
    # touch raw token values directly.
    assert "_token_map.lookup" in src


# ─────────────────────────────────────────────────────────────────────────────
# v0.9 — service_identity attachment + TokenMap construction modes
# ─────────────────────────────────────────────────────────────────────────────


def _make_app_with_map(token_map: TokenMap) -> FastAPI:
    """Build a throwaway app whose middleware uses an explicit TokenMap.

    The route handler reads ``request.scope[SCOPE_SERVICE_IDENTITY]``
    directly. Pure-ASGI scope propagation works through the auth
    middleware → starlette router; using a BaseHTTPMiddleware capture
    layer would break it (separate task, scope dict not shared).
    """
    app = FastAPI()
    app.add_middleware(BSSApiTokenMiddleware, token_map=token_map)

    @app.get("/")
    async def root(request: Request):
        return {"identity": request.scope.get(SCOPE_SERVICE_IDENTITY)}

    return app


PORTAL_TOKEN = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"


async def test_service_identity_attached_for_default_token() -> None:
    env = {"BSS_API_TOKEN": TEST_TOKEN}
    app = _make_app_with_map(load_token_map_from_env(env))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/", headers={"X-BSS-API-Token": TEST_TOKEN})
        assert r.status_code == 200
        assert r.json()["identity"] == "default"


async def test_service_identity_attached_for_named_token() -> None:
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": PORTAL_TOKEN}
    app = _make_app_with_map(load_token_map_from_env(env))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/", headers={"X-BSS-API-Token": PORTAL_TOKEN})
        assert r.status_code == 200
        assert r.json()["identity"] == "portal_self_serve"


async def test_unknown_token_returns_401_no_identity_attached() -> None:
    env = {"BSS_API_TOKEN": TEST_TOKEN}
    app = _make_app_with_map(load_token_map_from_env(env))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/", headers={"X-BSS-API-Token": "wrong"})
        assert r.status_code == 401
        # Identity capture middleware never ran; no body to assert.


async def test_separate_service_identity_header_is_not_trusted() -> None:
    """The whole point: caller cannot assert their own identity.

    Even if a request carries ``X-BSS-Service-Identity: portal``, the
    middleware must derive identity from the validated token alone.
    """
    env = {"BSS_API_TOKEN": TEST_TOKEN, "BSS_PORTAL_SELF_SERVE_API_TOKEN": PORTAL_TOKEN}
    app = _make_app_with_map(load_token_map_from_env(env))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Token is the default's, but caller asserts portal identity.
        r = await c.get(
            "/",
            headers={
                "X-BSS-API-Token": TEST_TOKEN,
                "X-BSS-Service-Identity": "portal",  # MUST be ignored
            },
        )
        assert r.status_code == 200
        # Identity must reflect the validated token (default), NOT the header.
        assert r.json()["identity"] == "default"


async def test_middleware_no_args_loads_from_env(monkeypatch) -> None:
    """v0.3 backwards-compat: ``add_middleware(BSSApiTokenMiddleware)`` works.

    No-arg construction loads the token map from os.environ at
    construction time. The 9 services rely on this — none of them
    pass a TokenMap explicitly today.
    """
    monkeypatch.setenv("BSS_API_TOKEN", TEST_TOKEN)
    monkeypatch.delenv("BSS_PORTAL_SELF_SERVE_API_TOKEN", raising=False)

    app = FastAPI()
    app.add_middleware(BSSApiTokenMiddleware)

    @app.get("/")
    async def root():
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/", headers={"X-BSS-API-Token": TEST_TOKEN})
        assert r.status_code == 200


async def test_middleware_token_kwarg_backwards_compat() -> None:
    """v0.3 ``token=`` kwarg still works — wraps a single-entry map internally."""
    app = FastAPI()
    app.add_middleware(BSSApiTokenMiddleware, token=TEST_TOKEN)

    @app.get("/")
    async def root():
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/", headers={"X-BSS-API-Token": TEST_TOKEN})
        assert r.status_code == 200
