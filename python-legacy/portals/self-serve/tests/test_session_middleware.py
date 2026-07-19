"""PortalSessionMiddleware + security deps + public-route allowlist.

Two engines are at play here, deliberately:

1. The test app's lifespan creates its own engine (bound to the
   TestClient's event loop) — that's what services the middleware.
2. The async fixtures use a SEPARATE engine to seed identity/session
   rows before the test runs.

Sharing a single engine across both blows up under
``RuntimeError: got Future <...> attached to a different loop``
because TestClient spins each test on a fresh asyncio loop and
asyncpg connections are loop-bound.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Pepper required by validate_pepper_present at app startup.
os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
os.environ.setdefault("BSS_PORTAL_EMAIL_ADAPTER", "noop")
os.environ.setdefault("BSS_PORTAL_EMAIL_PROVIDER", "noop")  # v0.14 — both names handled
os.environ.setdefault("BSS_PORTAL_DEV_INSECURE_COOKIE", "1")

from bss_clock import advance, freeze  # noqa: E402
from bss_clock.clock import reset_for_tests as _reset_clock  # noqa: E402
from bss_portal_auth.test_helpers import create_test_session  # noqa: E402
from bss_self_serve.middleware import PORTAL_SESSION_COOKIE  # noqa: E402
from bss_self_serve.middleware.session import (  # noqa: E402
    PortalSessionMiddleware,
    build_clear_cookie,
    build_session_cookie,
)
from bss_self_serve.security import (  # noqa: E402
    PUBLIC_EXACT_PATHS,
    PUBLIC_PATH_PREFIXES,
    install_redirect_handlers,
    is_public_path,
    requires_linked_customer,
    requires_session,
    requires_verified_email,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


class _DbSettings(BaseSettings):
    BSS_DB_URL: str = ""
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@pytest.fixture(autouse=True)
def _clock():
    _reset_clock()
    yield
    _reset_clock()


@pytest.fixture
def db_url() -> str:
    url = _DbSettings().BSS_DB_URL or os.environ.get("BSS_DB_URL", "")
    if not url:
        pytest.fail("BSS_DB_URL is not set. Export it or add to .env.")
    return url


@pytest_asyncio.fixture
async def seed_db(db_url: str):
    """Yields an async-session-factory bound to a setup engine.

    Used by tests to create identity/session rows BEFORE the TestClient
    spins up. Tests then attach the cookie value and exercise the app.
    The setup engine is disposed after the test.
    """
    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text(
            "TRUNCATE portal_auth.login_attempt, portal_auth.session, "
            "portal_auth.login_token, portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    yield factory
    async with factory() as s:
        await s.execute(text(
            "TRUNCATE portal_auth.login_attempt, portal_auth.session, "
            "portal_auth.login_token, portal_auth.identity RESTART IDENTITY CASCADE"
        ))
        await s.commit()
    await engine.dispose()


def _build_app_with_lifespan(db_url: str) -> FastAPI:
    """Minimal app whose lifespan creates its own engine (TestClient-loop bound)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_async_engine(db_url, pool_size=2, max_overflow=2)
        app.state.db_session_factory = async_sessionmaker(
            engine, expire_on_commit=False
        )
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(PortalSessionMiddleware)
    install_redirect_handlers(app)

    @app.get("/welcome", response_class=HTMLResponse)
    async def welcome() -> str:
        return "<h1>welcome</h1>"

    @app.get("/plans", response_class=HTMLResponse)
    async def plans() -> str:
        return "<h1>plans</h1>"

    @app.get("/auth/login", response_class=HTMLResponse)
    async def login_form() -> str:
        return "<h1>login</h1>"

    @app.get("/whoami")
    async def whoami(session=Depends(requires_session)) -> dict[str, str]:
        return {"identity_id": session.identity_id, "session_id": session.id}

    @app.get("/customer-only")
    async def customer_only(
        customer_id: str = Depends(requires_linked_customer),
    ) -> dict[str, str]:
        return {"customer_id": customer_id}

    @app.get("/verified-only")
    async def verified_only(
        identity=Depends(requires_verified_email),
    ) -> dict[str, str]:
        return {"email": identity.email}

    return app


@pytest.fixture
def client_factory(db_url: str):
    """Returns a callable that builds a fresh TestClient per use."""

    def _factory():
        app = _build_app_with_lifespan(db_url)
        return TestClient(app)

    return _factory


# ── Public allowlist (pure helpers — no DB) ──────────────────────────────


def test_public_exact_paths_includes_welcome_and_plans():
    assert "/welcome" in PUBLIC_EXACT_PATHS
    assert "/plans" in PUBLIC_EXACT_PATHS


def test_public_path_prefixes_include_auth_and_static():
    assert "/auth/" in PUBLIC_PATH_PREFIXES
    assert "/static/" in PUBLIC_PATH_PREFIXES
    assert "/portal-ui/static/" in PUBLIC_PATH_PREFIXES


def test_is_public_path_matches_exact_and_prefix():
    assert is_public_path("/welcome") is True
    assert is_public_path("/auth/login") is True
    assert is_public_path("/auth/check-email") is True
    assert is_public_path("/static/css/portal.css") is True
    assert is_public_path("/portal-ui/static/js/htmx.min.js") is True
    assert is_public_path("/") is False
    assert is_public_path("/signup/PLAN_M") is False


# ── Cookie builder (no DB) ───────────────────────────────────────────────


def test_session_cookie_has_secure_attrs_when_insecure_cookie_off(monkeypatch):
    """Production-shape: BSS_PORTAL_DEV_INSECURE_COOKIE=0 -> Secure flag on.

    Setenv-explicit (not delenv) because pydantic-settings reads .env
    which now defaults the var to 1 for compose dev. Production
    deployments override to 0; this test pins that path.
    """
    monkeypatch.setenv("BSS_PORTAL_DEV_INSECURE_COOKIE", "0")
    cookie = build_session_cookie("abc123")
    assert "bss_portal_session=abc123" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=" in cookie


def test_session_cookie_drops_secure_in_dev_mode(monkeypatch):
    monkeypatch.setenv("BSS_PORTAL_DEV_INSECURE_COOKIE", "1")
    cookie = build_session_cookie("abc123")
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_clear_cookie_zeroes_max_age():
    cookie = build_clear_cookie()
    assert "bss_portal_session=" in cookie
    assert "Max-Age=0" in cookie
    assert "Expires=" in cookie


# ── Middleware behaviour with a real app + real DB ───────────────────────


def test_no_cookie_leaves_state_none_on_public_route(client_factory, seed_db):
    with client_factory() as c:
        resp = c.get("/welcome")
        assert resp.status_code == 200
        assert "welcome" in resp.text


def test_protected_route_redirects_to_login_without_session(client_factory, seed_db):
    with client_factory() as c:
        resp = c.get("/whoami", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/auth/login")


def test_protected_route_with_unknown_cookie_still_redirects(client_factory, seed_db):
    with client_factory() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, "no-such-cookie")
        resp = c.get("/whoami", follow_redirects=False)
        assert resp.status_code == 303


@pytest.mark.asyncio
async def test_session_cookie_resolves_to_state_session_and_identity(
    seed_db, client_factory
):
    async with seed_db() as db:
        sess, identity = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id
        identity_id = identity.id

    with client_factory() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.get("/whoami")
        assert resp.status_code == 200
        body = resp.json()
        assert body["identity_id"] == identity_id
        assert body["session_id"] == sess_id


@pytest.mark.asyncio
async def test_revoked_session_cookie_redirects_back_to_login(
    seed_db, client_factory
):
    from bss_portal_auth import revoke_session
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await revoke_session(db, sess.id)
        await db.commit()
        sess_id = sess.id

    with client_factory() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.get("/whoami", follow_redirects=False)
        assert resp.status_code == 303


@pytest.mark.asyncio
async def test_session_rotates_after_half_ttl(seed_db, client_factory):
    """Past TTL/2 the middleware should mint a new session and Set-Cookie."""
    freeze()
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    advance(timedelta(hours=13))  # past 12h half of 24h TTL

    with client_factory() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.get("/whoami")
        assert resp.status_code == 200
        new_id = resp.json()["session_id"]
        assert new_id != sess_id
        set_cookie = resp.headers.get("set-cookie", "")
        assert PORTAL_SESSION_COOKIE in set_cookie
        assert new_id in set_cookie


# ── Dependency-specific behaviour ────────────────────────────────────────


@pytest.mark.asyncio
async def test_requires_linked_customer_redirects_for_unlinked_identity(
    seed_db, client_factory
):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@x.sg", customer_id=None, verified=True
        )
        await db.commit()
        sess_id = sess.id

    with client_factory() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.get("/customer-only", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/auth/login")


@pytest.mark.asyncio
async def test_requires_linked_customer_returns_id_for_linked_identity(
    seed_db, client_factory
):
    async with seed_db() as db:
        sess, _ = await create_test_session(
            db, email="ada@x.sg", customer_id="CUST-042", verified=True
        )
        await db.commit()
        sess_id = sess.id

    with client_factory() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.get("/customer-only")
        assert resp.status_code == 200
        assert resp.json() == {"customer_id": "CUST-042"}


@pytest.mark.asyncio
async def test_requires_verified_email_passes_for_verified_session(
    seed_db, client_factory
):
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg", verified=True)
        await db.commit()
        sess_id = sess.id

    with client_factory() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.get("/verified-only")
        assert resp.status_code == 200
        assert resp.json() == {"email": "ada@x.sg"}


@pytest.mark.asyncio
async def test_requires_verified_email_rejects_unverified_session(
    seed_db, client_factory
):
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg", verified=False)
        await db.commit()
        sess_id = sess.id

    with client_factory() as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.get("/verified-only", follow_redirects=False)
        assert resp.status_code == 303
