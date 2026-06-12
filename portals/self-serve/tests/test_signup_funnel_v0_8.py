"""Account-first signup funnel tests (v0.8 + v0.11).

v0.11 migrates the funnel from orchestrator-mediated to direct-API
calls from route handlers. The auth gating from v0.8 still applies —
that's the common ground covered here:

* /welcome and /plans are public (no session required).
* /signup/{plan}, /signup/{plan}/msisdn, POST /signup all redirect
  to /auth/login when no session is present.

The atomic ``link_to_customer`` invariant from v0.8 is preserved:
the moment ``crm.create_customer`` returns a CUST-* id, the verified
identity is bound to it (atomically with the customer write —
abandoning mid-chain still leaves the (identity, customer) pair so a
returning visitor under the same email reuses their record).

The full direct-write chain (POST /signup → /signup/step/{kyc,cof,
order,poll} → /confirmation) is exercised end-to-end below.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

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
from bss_models import Identity  # noqa: E402
from bss_portal_auth.test_helpers import create_test_session  # noqa: E402
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


# ── Public allowlist (no session required) ───────────────────────────────


def test_welcome_is_public(seed_db, fake_clients):
    with patch(
        "bss_self_serve.routes.welcome.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            resp = c.get("/welcome")
            assert resp.status_code == 200
            assert "bss-cli self-serve" in resp.text
            # Anonymous visitor sees the Sign-in CTA, not "My account".
            assert "Sign in" in resp.text
            assert "/auth/login" in resp.text


def test_plans_is_public(seed_db, fake_clients):
    with patch(
        "bss_self_serve.routes.welcome.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            resp = c.get("/plans")
            assert resp.status_code == 200
            assert "PLAN_M" in resp.text
            # Anonymous CTA bounces through /auth/login with next= preserved.
            assert "/auth/login?next=/signup/PLAN_M/msisdn" in resp.text


# ── Gated entry points redirect when no session ──────────────────────────


def test_signup_form_without_session_redirects_to_login(seed_db, fake_clients):
    with patch(
        "bss_self_serve.routes.signup.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            resp = c.get("/signup/PLAN_M?msisdn=90000042", follow_redirects=False)
            assert resp.status_code == 303
            loc = resp.headers["location"]
            assert loc.startswith("/auth/login")
            # next= preserves the originating path so post-login lands here again
            assert "next=" in loc
            assert "PLAN_M" in loc


def test_msisdn_picker_without_session_redirects_to_login(seed_db, fake_clients):
    with patch(
        "bss_self_serve.routes.msisdn_picker.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            resp = c.get("/signup/PLAN_M/msisdn", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/auth/login")


def test_signup_post_without_session_redirects_to_login(seed_db, fake_clients):
    with patch(
        "bss_self_serve.routes.signup.get_clients", return_value=fake_clients
    ):
        app = create_app(Settings())
        with TestClient(app) as c:
            resp = c.post(
                "/signup",
                data={
                    "plan": "PLAN_M",
                    "name": "Ada",
                    "email": "ada@x.sg",
                    "phone": "+6590001234",
                    "msisdn": "90000042",
                    "card_pan": "4242424242424242",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"].startswith("/auth/login")


# ── Direct-write chain end-to-end (v0.11) ────────────────────────────────


def _patch_clients(fake_clients):
    """All route modules that consume get_clients() in the direct path."""
    return [
        patch(
            "bss_self_serve.routes.welcome.get_clients", return_value=fake_clients
        ),
        patch(
            "bss_self_serve.routes.signup.get_clients", return_value=fake_clients
        ),
        patch(
            "bss_self_serve.routes.activation.get_clients", return_value=fake_clients
        ),
        patch(
            "bss_self_serve.routes.confirmation.get_clients", return_value=fake_clients
        ),
        patch(
            "bss_self_serve.routes.msisdn_picker.get_clients",
            return_value=fake_clients,
        ),
        patch(
            "bss_self_serve.routes.landing.get_clients", return_value=fake_clients
        ),
    ]


@pytest.mark.asyncio
async def test_link_to_customer_runs_when_customer_create_returns_id(
    seed_db, fake_clients
):
    """v0.11 — the moment crm.create_customer returns CUST-*, the verified
    identity is linked to it. POST /signup is the single write site for
    that linking; no separate SSE stream is involved."""
    async with seed_db() as db:
        sess, identity = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sid = sess.id
        identity_id = identity.id

    fake_clients.crm.next_customer_id = "CUST-042"

    patches = _patch_clients(fake_clients)
    for p in patches:
        p.start()
    try:
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            resp = c.post(
                "/signup",
                data={
                    "plan": "PLAN_M",
                    "name": "Ada",
                    "phone": "+6590001234",
                    "msisdn": "90000042",
                    "card_pan": "4242424242424242",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
    finally:
        for p in patches:
            p.stop()

    # Identity is now linked to CUST-042 — atomic with the create.
    async with seed_db() as db:
        row = (
            await db.execute(select(Identity).where(Identity.id == identity_id))
        ).scalar_one()
        assert row.customer_id == "CUST-042"
        assert row.status == "registered"


@pytest.mark.asyncio
async def test_link_to_customer_persists_when_visitor_abandons_after_customer_create(
    seed_db, fake_clients
):
    """Mid-flow bail: customer.create succeeded, the visitor closes the
    browser before completing KYC.

    The identity is linked to the customer the moment POST /signup
    returns — the link survives even if the rest of the chain never
    runs. Returning visitor under the same email gets their existing
    customer record.
    """
    async with seed_db() as db:
        sess, identity = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sid = sess.id
        identity_id = identity.id

    fake_clients.crm.next_customer_id = "CUST-042"

    patches = _patch_clients(fake_clients)
    for p in patches:
        p.start()
    try:
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            # POST /signup commits the customer + links — then the test
            # never fires the rest of the chain (no /signup/step/kyc).
            resp = c.post(
                "/signup",
                data={
                    "plan": "PLAN_M",
                    "name": "Ada",
                    "phone": "+6590001234",
                    "msisdn": "90000042",
                    "card_pan": "4242424242424242",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
    finally:
        for p in patches:
            p.stop()

    async with seed_db() as db:
        row = (
            await db.execute(select(Identity).where(Identity.id == identity_id))
        ).scalar_one()
        # Linked even though the chain never finished.
        assert row.customer_id == "CUST-042"


@pytest.mark.asyncio
async def test_link_to_customer_idempotent_on_retry(seed_db, fake_clients):
    """Re-running POST /signup with the same identity re-creates a new
    CUST-* (fake), but the identity stays linked to the FIRST one — the
    portal-auth link is 1:1 and not reassignable from this surface.
    The second call's link attempt is a no-op (link_to_customer raises
    ValueError when re-linked to a different customer; the route
    swallows it as a warning so the chain still runs)."""
    async with seed_db() as db:
        sess, identity = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sid = sess.id
        identity_id = identity.id

    fake_clients.crm.next_customer_id = "CUST-042"

    patches = _patch_clients(fake_clients)
    for p in patches:
        p.start()
    try:
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)
            for _ in range(2):
                # Each call returns a fresh CUST-* from the fake; the
                # link logic should keep the identity bound to the first.
                resp = c.post(
                    "/signup",
                    data={
                        "plan": "PLAN_M",
                        "name": "Ada",
                        "phone": "+6590001234",
                        "msisdn": "90000042",
                        "card_pan": "4242424242424242",
                    },
                    follow_redirects=False,
                )
                assert resp.status_code == 303
    finally:
        for p in patches:
            p.stop()

    # Still linked to CUST-042 from the first call. No exception, no
    # double-linking — the route handler caught the second link
    # attempt's ValueError and continued.
    async with seed_db() as db:
        row = (
            await db.execute(select(Identity).where(Identity.id == identity_id))
        ).scalar_one()
        assert row.customer_id == "CUST-042"


# ── End-to-end happy path through the direct-write chain ─────────────────


@pytest.mark.asyncio
async def test_direct_write_chain_completes_without_orchestrator(
    seed_db, fake_clients
):
    """v0.11 happy path: POST /signup → /signup/step/kyc → /signup/step/cof
    → /signup/step/order → /signup/step/poll → HX-Redirect to /confirmation.

    No ``astream_once`` involved; each step is one bss-clients call (or
    zero for the poll). Asserts each fake's call list at the end."""
    async with seed_db() as db:
        sess, _identity = await create_test_session(db, email="ada@x.sg")
        await db.commit()
        sid = sess.id

    fake_clients.crm.next_customer_id = "CUST-042"
    # COM poll: one acknowledged tick, then completed (with SUB-007).
    fake_clients.com.next_order_states = ["acknowledged", "completed"]
    fake_clients.com.next_subscription_id = "SUB-007"
    fake_clients.com.next_activation_code = "LPA:1$smdp$activation-code-007"

    patches = _patch_clients(fake_clients)
    for p in patches:
        p.start()
    try:
        app = create_app(Settings())
        with TestClient(app) as c:
            c.cookies.set(PORTAL_SESSION_COOKIE, sid)

            # Step 1: customer.create
            resp = c.post(
                "/signup",
                data={
                    "plan": "PLAN_M",
                    "name": "Ada",
                    "phone": "+6590001234",
                    "msisdn": "90000042",
                    "card_pan": "4242424242424242",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303
            location = resp.headers["location"]
            from urllib.parse import parse_qs, urlparse
            session_id = parse_qs(urlparse(location).query)["session"][0]

            # Step 2: attest_kyc
            r2 = c.post(
                f"/signup/step/kyc?session={session_id}", follow_redirects=False
            )
            assert r2.status_code == 200
            assert "attest KYC" in r2.text

            # Step 3: payment.add_card
            r3 = c.post(
                f"/signup/step/cof?session={session_id}", follow_redirects=False
            )
            assert r3.status_code == 200

            # Step 4: com.create_order + submit_order
            r4 = c.post(
                f"/signup/step/order?session={session_id}", follow_redirects=False
            )
            assert r4.status_code == 200

            # Step 5a: poll once — order still acknowledged.
            r5 = c.get(
                f"/signup/step/poll?session={session_id}", follow_redirects=False
            )
            assert r5.status_code == 200
            # No HX-Redirect yet.
            assert "HX-Redirect" not in r5.headers and "hx-redirect" not in r5.headers

            # Step 5b: poll again — order completed. The route arms
            # ``redirect_armed`` and renders the celebration fragment
            # with a 1.5s delayed re-trigger; no HX-Redirect yet so
            # the user sees the chain finish before navigation.
            r6 = c.get(
                f"/signup/step/poll?session={session_id}", follow_redirects=False
            )
            assert r6.status_code == 200
            assert "HX-Redirect" not in r6.headers and "hx-redirect" not in r6.headers
            assert "Activated" in r6.text  # celebration fragment

            # Step 5c: poll one more time — redirect_armed is now true,
            # the route emits HX-Redirect to /confirmation.
            r7 = c.get(
                f"/signup/step/poll?session={session_id}", follow_redirects=False
            )
            assert r7.status_code == 200
            redirect = r7.headers.get("HX-Redirect") or r7.headers.get("hx-redirect")
            assert redirect is not None
            assert redirect.startswith("/confirmation/SUB-007")
    finally:
        for p in patches:
            p.stop()

    # Each fake's call list reflects one direct write per step.
    assert len(fake_clients.crm.create_customer_calls) == 1
    assert len(fake_clients.crm.attest_kyc_calls) == 1
    assert len(fake_clients.payment.create_calls) == 1
    assert len(fake_clients.com.create_calls) == 1
    assert len(fake_clients.com.submit_calls) == 1
