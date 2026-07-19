"""``/auth/*`` route tests — login, check-email, verify, logout, step-up.

Spins the full self-serve portal app (so middleware + lifespan run for
real) but uses NoopEmailAdapter so OTPs / magic links can be inspected
in-process via ``last_login_codes`` / ``last_step_up_code`` rather
than tailing a file.

DB rows are seeded + scrubbed via the same dual-engine pattern the
session-middleware tests use (separate engine for fixture data; the
app's lifespan creates its own engine bound to the TestClient loop).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "BSS_PORTAL_TOKEN_PEPPER",
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
)
os.environ.setdefault("BSS_PORTAL_EMAIL_ADAPTER", "noop")
os.environ.setdefault("BSS_PORTAL_EMAIL_PROVIDER", "noop")  # v0.14 — both names handled
os.environ.setdefault("BSS_PORTAL_DEV_INSECURE_COOKIE", "1")

from bss_clock.clock import reset_for_tests as _reset_clock  # noqa: E402
from bss_models import LoginAttempt  # noqa: E402
from bss_models import Session as SessionRow
from bss_portal_auth.test_helpers import (  # noqa: E402
    create_test_session,
    last_login_codes,
    last_step_up_code,
)
from bss_self_serve.config import Settings  # noqa: E402
from bss_self_serve.main import create_app  # noqa: E402
from bss_self_serve.middleware import PORTAL_SESSION_COOKIE  # noqa: E402

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
    # Settings (used by create_app) reads `bss_db_url`, lowercased — pass
    # via env so its pydantic-settings autoload picks it up.
    os.environ["BSS_DB_URL"] = url
    return url


@pytest_asyncio.fixture
async def seed_db(db_url: str):
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


@pytest.fixture
def client(db_url: str):
    """Live portal app. Each test owns its TestClient lifecycle."""
    app = create_app(Settings())
    with TestClient(app) as c:
        yield c


def _adapter(client: TestClient):
    return client.app.state.email_adapter


# ── /auth/login ──────────────────────────────────────────────────────────


def test_login_form_renders(client, seed_db):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    body = resp.text
    assert "Sign in" in body
    assert 'name="email"' in body
    # next_path defaults to "/"
    assert 'value="/"' in body


def test_login_form_carries_next_through(client, seed_db):
    resp = client.get("/auth/login?next=/signup/PLAN_M")
    assert resp.status_code == 200
    assert 'value="/signup/PLAN_M"' in resp.text


def test_login_form_strips_open_redirect_target(client, seed_db):
    """safe_next_path defangs ``next=//evil.example``."""
    resp = client.get("/auth/login?next=//evil.example/x")
    # Falls back to "/", never echoes the //evil string.
    assert resp.status_code == 200
    assert "evil.example" not in resp.text


def test_login_post_with_bad_email_re_renders(client, seed_db):
    resp = client.post(
        "/auth/login", data={"email": "not-an-email", "next": "/"}, follow_redirects=False
    )
    assert resp.status_code == 400
    # Apostrophe is HTML-escaped to &#39; via Jinja autoescape.
    assert "look like an email" in resp.text


def test_login_post_with_valid_email_redirects_to_check_email(client, seed_db):
    resp = client.post(
        "/auth/login",
        data={"email": "ada@example.sg", "next": "/signup/PLAN_M"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    # urlencoded form: '@' -> '%40', '/' -> '%2F'
    assert loc.startswith("/auth/check-email?")
    assert "email=ada%40example.sg" in loc
    assert "next=%2Fsignup%2FPLAN_M" in loc

    rec = last_login_codes(_adapter(client), "ada@example.sg")
    assert "otp" in rec and len(rec["otp"]) == 6
    assert "magic_link" in rec and len(rec["magic_link"]) == 32


def test_login_post_with_plus_addressed_email_preserves_plus(client, seed_db):
    """Regression: ``cheekong+test001@gmail.com`` must round-trip through
    the redirect without ``+`` being mangled to a space. Earlier
    f-string-built redirect URLs caused FastAPI's ``Query(...)`` to
    decode ``+`` as space (urlencoded-form convention), which produced
    ``no_such_identity`` on every OTP submit. Fixed in v0.15 by routing
    through ``urllib.parse.urlencode``.
    """
    plus_email = "ada+test001@example.sg"
    resp = client.post(
        "/auth/login",
        data={"email": plus_email, "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    # '+' must be percent-encoded as %2B; raw '+' or ' ' would re-decode wrong.
    assert "email=ada%2Btest001%40example.sg" in loc, loc
    assert "ada+test001" not in loc.replace("%2B", "_"), loc

    # Following the redirect must yield the same email back, intact.
    follow = client.get(loc)
    assert follow.status_code == 200
    # The pre-filled (hidden) form input should carry the '+' restored.
    assert "ada+test001@example.sg" in follow.text or "a**+test001@example.sg" in follow.text


# ── /auth/check-email ────────────────────────────────────────────────────


def test_check_email_form_renders_masked_email(client, seed_db):
    resp = client.get("/auth/check-email?email=ada@example.sg")
    assert resp.status_code == 200
    # Local-part masked (`ada` -> `a**`); domain visible.
    assert "a**@example.sg" in resp.text
    assert "Code expires in 15 minutes" in resp.text


def test_check_email_post_with_bad_otp_re_renders(client, seed_db):
    client.post("/auth/login", data={"email": "ada@x.sg"}, follow_redirects=False)
    resp = client.post(
        "/auth/check-email",
        data={"email": "ada@x.sg", "code": "000000", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "Incorrect or expired" in resp.text


def test_check_email_post_with_valid_otp_sets_cookie_and_redirects(client, seed_db):
    client.post("/auth/login", data={"email": "ada@x.sg"}, follow_redirects=False)
    otp = last_login_codes(_adapter(client), "ada@x.sg")["otp"]

    resp = client.post(
        "/auth/check-email",
        data={"email": "ada@x.sg", "code": otp, "next": "/dashboard"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"

    set_cookie = resp.headers.get("set-cookie", "")
    assert PORTAL_SESSION_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie


# ── /auth/verify (magic-link) ────────────────────────────────────────────


def test_magic_link_verify_succeeds(client, seed_db):
    client.post("/auth/login", data={"email": "ada@x.sg"}, follow_redirects=False)
    rec = last_login_codes(_adapter(client), "ada@x.sg")
    magic = rec["magic_link"]

    resp = client.get(
        f"/auth/verify?email=ada@x.sg&token={magic}&next=/",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert PORTAL_SESSION_COOKIE in resp.headers.get("set-cookie", "")


def test_magic_link_verify_with_bad_token_bounces_back_to_login(client, seed_db):
    client.post("/auth/login", data={"email": "ada@x.sg"}, follow_redirects=False)
    resp = client.get(
        "/auth/verify?email=ada@x.sg&token=bogus&next=/dashboard",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth/login")
    assert "next=/dashboard" in resp.headers["location"]


# ── /auth/logout ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_logout_revokes_session_and_clears_cookie(seed_db, db_url: str):
    """Use a separate engine to seed a live session, then exercise logout."""
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    app = create_app(Settings())
    with TestClient(app) as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.post("/auth/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/welcome"
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Max-Age=0" in set_cookie

    # The session row in DB has revoked_at set.
    async with seed_db() as db:
        row = (
            await db.execute(select(SessionRow).where(SessionRow.id == sess_id))
        ).scalar_one()
        assert row.revoked_at is not None


# ── /auth/step-up ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_up_form_renders_for_logged_in_user(seed_db, db_url: str):
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    app = create_app(Settings())
    with TestClient(app) as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.get("/auth/step-up?action=subscription.terminate&next=/sub/SUB-007")
        assert resp.status_code == 200
        assert "subscription.terminate" in resp.text
        # Pre-issue: shows "Email me a code"
        assert "Email me a code" in resp.text


@pytest.mark.asyncio
async def test_step_up_start_issues_otp_via_email(seed_db, db_url: str):
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    app = create_app(Settings())
    with TestClient(app) as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        resp = c.post(
            "/auth/step-up/start",
            data={"action": "subscription.terminate", "next": "/sub/SUB-007"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Code sent" in resp.text
        otp = last_step_up_code(c.app.state.email_adapter, "ada@x.sg", "subscription.terminate")
        assert otp is not None and len(otp) == 6


@pytest.mark.asyncio
async def test_step_up_verify_sets_grant_cookie_and_redirects(seed_db, db_url: str):
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    app = create_app(Settings())
    with TestClient(app) as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        c.post(
            "/auth/step-up/start",
            data={"action": "subscription.terminate", "next": "/sub/SUB-007"},
        )
        otp = last_step_up_code(
            c.app.state.email_adapter, "ada@x.sg", "subscription.terminate"
        )

        resp = c.post(
            "/auth/step-up",
            data={
                "code": otp,
                "action": "subscription.terminate",
                "next": "/sub/SUB-007",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/sub/SUB-007"
        set_cookie = resp.headers.get("set-cookie", "")
        assert "bss_portal_step_up=" in set_cookie


@pytest.mark.asyncio
async def test_step_up_verify_with_wrong_otp_re_renders_with_error(seed_db, db_url: str):
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    app = create_app(Settings())
    with TestClient(app) as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        c.post(
            "/auth/step-up/start",
            data={"action": "subscription.terminate", "next": "/sub/SUB-007"},
        )
        resp = c.post(
            "/auth/step-up",
            data={
                "code": "000000",
                "action": "subscription.terminate",
                "next": "/sub/SUB-007",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "Incorrect or expired" in resp.text


# ── /auth/step-up replay — POST-body stash ──────────────────────────────


@pytest.mark.asyncio
async def test_step_up_verify_renders_replay_when_stash_present(
    seed_db, db_url: str
):
    """If a POST stashed its body before the step-up bounce, /auth/step-up
    verify renders the auto-replay template (with target_url +
    hidden inputs) instead of a 303. The fresh grant cookie still
    rides on the same response.
    """
    from bss_portal_auth import stash_pending_action

    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    async with seed_db() as db:
        await stash_pending_action(
            db,
            session_id=sess_id,
            action_label="name_update",
            target_url="/profile/contact/name/update",
            payload={"given_name": "Augusta", "family_name": "Byron"},
        )
        await db.commit()

    app = create_app(Settings())
    with TestClient(app) as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        c.post(
            "/auth/step-up/start",
            data={"action": "name_update", "next": "/profile/contact"},
        )
        otp = last_step_up_code(c.app.state.email_adapter, "ada@x.sg", "name_update")

        resp = c.post(
            "/auth/step-up",
            data={"code": otp, "action": "name_update", "next": "/profile/contact"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        body = resp.text
        # Replay form posts to the original POST URL with the stashed
        # values as hidden inputs.
        assert 'action="/profile/contact/name/update"' in body
        assert 'name="given_name"' in body and 'value="Augusta"' in body
        assert 'name="family_name"' in body and 'value="Byron"' in body
        # And the grant cookie is set on this same response so the
        # replay submit carries it.
        assert "bss_portal_step_up=" in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_step_up_verify_with_no_stash_redirects_303(seed_db, db_url: str):
    """No stash row → existing behaviour (303 to ``next``)."""
    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    app = create_app(Settings())
    with TestClient(app) as c:
        c.cookies.set(PORTAL_SESSION_COOKIE, sess_id)
        c.post(
            "/auth/step-up/start",
            data={"action": "name_update", "next": "/profile/contact"},
        )
        otp = last_step_up_code(c.app.state.email_adapter, "ada@x.sg", "name_update")

        resp = c.post(
            "/auth/step-up",
            data={"code": otp, "action": "name_update", "next": "/profile/contact"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/profile/contact"


@pytest.mark.asyncio
async def test_step_up_pending_action_is_one_shot(seed_db, db_url: str):
    """Consuming a stashed pending action twice returns None on the
    second call — the row is single-use, same as the grant token.
    """
    from bss_portal_auth import (
        consume_pending_action,
        stash_pending_action,
    )

    async with seed_db() as db:
        sess, _ = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sess_id = sess.id

    async with seed_db() as db:
        await stash_pending_action(
            db,
            session_id=sess_id,
            action_label="name_update",
            target_url="/profile/contact/name/update",
            payload={"given_name": "X", "family_name": "Y"},
        )
        await db.commit()

    async with seed_db() as db:
        first = await consume_pending_action(
            db, session_id=sess_id, action_label="name_update"
        )
        await db.commit()
    async with seed_db() as db:
        second = await consume_pending_action(
            db, session_id=sess_id, action_label="name_update"
        )
        await db.commit()
    assert first is not None
    assert first.payload == {"given_name": "X", "family_name": "Y"}
    assert second is None


# ── Cross-route audit + rate-limit observability ─────────────────────────


@pytest.mark.asyncio
async def test_login_failure_is_audited(seed_db, db_url: str):
    """Verify failure path writes a login_attempt row with the right outcome."""
    app = create_app(Settings())
    with TestClient(app) as c:
        c.post("/auth/login", data={"email": "ada@x.sg"})
        c.post(
            "/auth/check-email",
            data={"email": "ada@x.sg", "code": "000000", "next": "/"},
            follow_redirects=False,
        )

    async with seed_db() as db:
        rows = (
            await db.execute(
                select(LoginAttempt).where(LoginAttempt.email == "ada@x.sg")
            )
        ).scalars().all()
        outcomes = [r.outcome for r in rows]
        assert "issued" in outcomes  # login_start
        assert "wrong_code" in outcomes  # login_verify

    _ = parse_qs  # silence unused-import lint when not needed
    _ = urlparse
